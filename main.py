import asyncio
import json
import sys
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import qasync
import websockets
from pynput import keyboard
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from screeninfo import get_monitors


@dataclass
class MonitorInfo:
    """Simple container for monitor geometry."""

    name: str
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_screeninfo(cls, monitor) -> "MonitorInfo":
        return cls(
            name=getattr(monitor, "name", "monitor"),
            x=monitor.x,
            y=monitor.y,
            width=monitor.width,
            height=monitor.height,
        )

    def rect(self) -> QRect:
        return QRect(self.x, self.y, self.width, self.height)


def list_monitors() -> List[MonitorInfo]:
    monitors: List[MonitorInfo] = []
    for idx, mon in enumerate(get_monitors()):
        name = getattr(mon, "name", f"Display {idx + 1}")
        monitors.append(
            MonitorInfo(
                name=name,
                x=mon.x,
                y=mon.y,
                width=mon.width,
                height=mon.height,
            )
        )
    return monitors or [
        MonitorInfo(name="Primary", x=0, y=0, width=1920, height=1080)
    ]


class MouseServer:
    """WebSocket broadcaster for mouse movement with UDP fast-path."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self.loop = loop
        self._host = host
        self._port = port
        self._clients: set[websockets.WebSocketServerProtocol] = set()
        self._server: Optional[websockets.server.Serve] = None
        self._udp_clients: Set[Tuple[str, int]] = set()
        self._udp_transport: Optional[asyncio.DatagramTransport] = None

    @property
    def address(self) -> str:
        return f"ws://{self._host}:{self._port}"

    async def start(self) -> None:
        if self._server:
            return

        async def handler(websocket: websockets.WebSocketServerProtocol):
            self._clients.add(websocket)
            try:
                async for message in websocket:
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    udp_port = payload.get("udp_port")
                    if isinstance(udp_port, int):
                        peer_ip = websocket.remote_address[0] if websocket.remote_address else None
                        if peer_ip:
                            self._udp_clients.add((peer_ip, udp_port))
            finally:
                self._clients.discard(websocket)

        self._server = await websockets.serve(handler, self._host, self._port)
        # UDP transport for faster broadcasting
        self._udp_transport, _ = await self.loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), local_addr=(self._host, 0)
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._udp_transport:
            self._udp_transport.close()
        self._server = None
        self._udp_transport = None
        self._udp_clients.clear()
        self._clients.clear()

    async def broadcast(self, payload: str) -> None:
        if self._clients:
            stale = []
            for client in tuple(self._clients):
                try:
                    await client.send(payload)
                except Exception:
                    stale.append(client)
            for client in stale:
                self._clients.discard(client)

        if self._udp_transport and self._udp_clients:
            data = payload.encode()
            for addr in tuple(self._udp_clients):
                try:
                    self._udp_transport.sendto(data, addr)
                except Exception:
                    self._udp_clients.discard(addr)


class OverlayWindow(QWidget):
    """Always-on-top overlay that traps the mouse and streams movement."""

    def __init__(
        self,
        monitor: MonitorInfo,
        server: MouseServer,
        loop: asyncio.AbstractEventLoop,
        opacity: float = 0.3,
    ) -> None:
        super().__init__()
        self.monitor = monitor
        self.server = server
        self.loop = loop
        self._opacity = opacity

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setWindowOpacity(self._opacity)

    def activate(self) -> None:
        rect = self.monitor.rect()
        self.setGeometry(rect)
        self.show()
        self.grabMouse()

    def deactivate(self) -> None:
        self.releaseMouse()
        self.hide()

    def set_overlay_opacity(self, value: float) -> None:
        self._opacity = value
        self.setWindowOpacity(value)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        overlay = QColor(30, 30, 30, int(255 * self._opacity))
        painter.fillRect(self.rect(), overlay)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        geo = self.geometry()
        global_pos = event.globalPosition().toPoint()
        clamped_x = max(geo.left(), min(global_pos.x(), geo.right()))
        clamped_y = max(geo.top(), min(global_pos.y(), geo.bottom()))
        if clamped_x != global_pos.x() or clamped_y != global_pos.y():
            QCursor.setPos(clamped_x, clamped_y)

        norm_x = (clamped_x - geo.left()) / geo.width()
        norm_y = (clamped_y - geo.top()) / geo.height()
        payload = json.dumps(
            {
                "x": norm_x,
                "y": norm_y,
                "screen": self.monitor.name,
                "width": geo.width(),
                "height": geo.height(),
            }
        )
        asyncio.create_task(self.server.broadcast(payload))


class ControlWindow(QWidget):
    """Small control panel to pick monitor, opacity, and toggle overlay."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.monitors = list_monitors()
        self.server = MouseServer(loop=self.loop)
        self.overlay: Optional[OverlayWindow] = None
        self.overlay_enabled = False
        self.hotkey = "<ctrl>+<alt>+m"
        self.hotkey_listener: Optional[keyboard.GlobalHotKeys] = None
        self._build_ui()
        self._start_hotkey_listener()

    def _build_ui(self) -> None:
        self.setWindowTitle("Cross Pointers - Host")
        layout = QVBoxLayout()

        info_label = QLabel("Select a monitor and press Start. Hotkey toggles overlay.")
        layout.addWidget(info_label)

        monitor_row = QHBoxLayout()
        monitor_row.addWidget(QLabel("Target monitor:"))
        self.monitor_combo = QComboBox()
        for mon in self.monitors:
            self.monitor_combo.addItem(
                f"{mon.name} ({mon.width}x{mon.height} @ {mon.x},{mon.y})"
            )
        monitor_row.addWidget(self.monitor_combo)
        layout.addLayout(monitor_row)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Overlay opacity:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 90)
        self.opacity_slider.setValue(30)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self.opacity_slider)
        layout.addLayout(opacity_row)

        self.status_label = QLabel(
            f"Server will listen on {self.server.address}. Hotkey {self.hotkey}"
        )
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start overlay")
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        self.setLayout(layout)
        self.resize(480, 160)

    def _start_hotkey_listener(self) -> None:
        self.hotkey_listener = keyboard.GlobalHotKeys(
            {self.hotkey: self._hotkey_toggle}
        )
        self.hotkey_listener.start()

    def _hotkey_toggle(self) -> None:
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.toggle_overlay()))

    def _on_start_clicked(self) -> None:
        asyncio.create_task(self.enable_overlay())

    def _on_stop_clicked(self) -> None:
        asyncio.create_task(self.disable_overlay())

    def _on_opacity_changed(self, value: int) -> None:
        if self.overlay:
            self.overlay.set_overlay_opacity(value / 100)

    async def toggle_overlay(self) -> None:
        if self.overlay_enabled:
            await self.disable_overlay()
        else:
            await self.enable_overlay()

    async def enable_overlay(self) -> None:
        await self.server.start()
        monitor = self.monitors[self.monitor_combo.currentIndex()]
        opacity_value = self.opacity_slider.value() / 100
        if self.overlay:
            self.overlay.deactivate()
            self.overlay.deleteLater()
        self.overlay = OverlayWindow(
            monitor=monitor, server=self.server, loop=self.loop, opacity=opacity_value
        )
        self.overlay.activate()
        self.overlay_enabled = True
        self.status_label.setText(
            f"Overlay ON ({monitor.name}). Server {self.server.address}. Hotkey {self.hotkey}"
        )

    async def disable_overlay(self) -> None:
        if self.overlay:
            self.overlay.deactivate()
            self.overlay.deleteLater()
            self.overlay = None
        self.overlay_enabled = False
        self.status_label.setText(
            f"Overlay OFF. Server {self.server.address}. Hotkey {self.hotkey}"
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        asyncio.create_task(self.server.stop())
        self.loop.call_soon(self.loop.stop)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ControlWindow(loop)
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
