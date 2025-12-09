import asyncio
import json
import contextlib
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

import qasync
import websockets
from pynput import keyboard
from PySide6.QtCore import QPoint, QTimer, QRect, Qt
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
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._pump_task: Optional[asyncio.Task] = None
        self._config: Dict[str, Dict[str, object]] = {
            "host": {"hot_edge": "none", "hot_velocity": 900, "hot_band": 16},
            "client": {"hot_edge": "none", "hot_velocity": 900, "hot_band": 16},
        }
        self._config_listener: Optional[Callable[[dict], None]] = None
        self._command_listener: Optional[Callable[[str, dict], None]] = None

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
                    msg_type = payload.get("type")
                    if msg_type == "hello":
                        config = payload.get("config")
                        role = payload.get("role", "client")
                        if isinstance(config, dict):
                            self._update_config(config, role=role, origin=websocket)
                        await self._send_config_to(websocket)
                    elif msg_type == "config_update":
                        config = payload.get("config")
                        role = payload.get("role", "client")
                        if isinstance(config, dict):
                            self._update_config(config, role=role, origin=websocket)
                    elif msg_type == "hot_edge_hit":
                        if self._command_listener:
                            self._command_listener("hot_edge_hit", payload)
            finally:
                self._clients.discard(websocket)

        self._server = await websockets.serve(handler, self._host, self._port)
        # UDP transport for faster broadcasting
        self._udp_transport, _ = await self.loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), local_addr=(self._host, 0)
        )
        self._pump_task = self.loop.create_task(self._pump())

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
        if self._pump_task:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        self._pump_task = None

    def set_config_listener(self, listener: Callable[[dict], None]) -> None:
        self._config_listener = listener

    def set_command_listener(self, listener: Callable[[str, dict], None]) -> None:
        self._command_listener = listener

    def set_hot_config(self, config: Dict[str, object]) -> None:
        self._update_config(config, role="host")

    def _update_config(
        self,
        config: Dict[str, object],
        role: str,
        origin: Optional[websockets.WebSocketServerProtocol] = None,
    ) -> None:
        if role not in {"host", "client"}:
            role = "client"
        target = self._config.setdefault(role, {})
        changed = False
        for key in ("hot_edge", "hot_velocity", "hot_band"):
            if key in config and config[key] != target.get(key):
                target[key] = config[key]
                changed = True
        if not changed:
            return
        if self._config_listener:
            self._config_listener(dict(self._config))
        self.loop.create_task(self._broadcast_config(origin=origin))

    async def _broadcast_config(
        self, origin: Optional[websockets.WebSocketServerProtocol] = None
    ) -> None:
        if not self._clients:
            return
        msg = json.dumps({"type": "config_update", "config": self._config})
        stale: list[websockets.WebSocketServerProtocol] = []
        for client in tuple(self._clients):
            if origin and client == origin:
                continue
            try:
                await client.send(msg)
            except Exception:
                stale.append(client)
        for client in stale:
            self._clients.discard(client)

    async def _send_config_to(self, websocket: websockets.WebSocketServerProtocol) -> None:
        try:
            await websocket.send(json.dumps({"type": "config_update", "config": self._config}))
        except Exception:
            self._clients.discard(websocket)

    async def _pump(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                is_keyboard = False
                try:
                    parsed = json.loads(payload)
                    action = parsed.get("action", "")
                    is_keyboard = action.startswith("key_")
                except Exception:
                    pass

                if self._clients:
                    stale = []
                    for client in tuple(self._clients):
                        try:
                            await client.send(payload)
                        except Exception:
                            stale.append(client)
                    for client in stale:
                        self._clients.discard(client)

                if self._udp_transport and self._udp_clients and not is_keyboard:
                    data = payload.encode()
                    for addr in tuple(self._udp_clients):
                        try:
                            self._udp_transport.sendto(data, addr)
                        except Exception:
                            self._udp_clients.discard(addr)
            finally:
                self._queue.task_done()

    def enqueue(self, payload: str) -> None:
        """Queue events with light backpressure; drop oldest moves if saturated."""
        if self._queue.full():
            # Drop the oldest move event to prioritize clicks.
            try:
                oldest = self._queue.get_nowait()
                if '"action": "move"' not in oldest:
                    # put it back if it was not a move
                    self._queue.put_nowait(oldest)
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # If still full, drop the new move silently.
            pass


def _button_name(button: Qt.MouseButton) -> str:
    mapping = {
        Qt.LeftButton: "left",
        Qt.RightButton: "right",
        Qt.MiddleButton: "middle",
        Qt.BackButton: "x1",
        Qt.ForwardButton: "x2",
    }
    return mapping.get(button, "unknown")


class OverlayWindow(QWidget):
    """Always-on-top overlay that traps the mouse and streams movement."""

    def __init__(
        self,
        monitor: MonitorInfo,
        server: MouseServer,
        loop: asyncio.AbstractEventLoop,
        opacity: float = 0.3,
        host_hot_edge: str = "none",
        host_hot_band: int = 16,
        client_hot_edge: str = "none",
        client_hot_band: int = 16,
    ) -> None:
        super().__init__()
        self.monitor = monitor
        self.server = server
        self.loop = loop
        self._opacity = opacity
        self.host_hot_edge = host_hot_edge
        self.host_hot_band = host_hot_band
        self.client_hot_edge = client_hot_edge
        self.client_hot_band = client_hot_band

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setWindowOpacity(self._opacity)
        self.setFocusPolicy(Qt.StrongFocus)

    def activate(self) -> None:
        rect = self.monitor.rect()
        self.setGeometry(rect)
        self.show()
        self.grabMouse()
        self.raise_()
        self.activateWindow()

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
        self._send_pointer_event(event.globalPosition().toPoint(), action="move")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._send_pointer_event(
            event.globalPosition().toPoint(),
            action="down",
            button=_button_name(event.button()),
        )

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._send_pointer_event(
            event.globalPosition().toPoint(),
            action="up",
            button=_button_name(event.button()),
        )

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return
        key_name, modifiers = self._normalize_key(event)
        if key_name == "esc" and "ctrl" in modifiers:
            return  # skip hotkey combination
        self._send_key_event("key_down", key_name, modifiers)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return
        key_name, modifiers = self._normalize_key(event)
        if key_name == "esc" and "ctrl" in modifiers:
            return
        self._send_key_event("key_up", key_name, modifiers)

    def update_hot_config(
        self,
        host_hot_edge: str,
        host_hot_band: int,
        client_hot_edge: str,
        client_hot_band: int,
    ) -> None:
        self.host_hot_edge = host_hot_edge
        self.host_hot_band = host_hot_band
        self.client_hot_edge = client_hot_edge
        self.client_hot_band = client_hot_band

    def _send_pointer_event(self, global_pos, action: str, button: Optional[str] = None) -> None:
        global_pos = self._maybe_wrap_position(global_pos)
        geo = self.geometry()
        clamped_x = max(geo.left(), min(global_pos.x(), geo.right()))
        clamped_y = max(geo.top(), min(global_pos.y(), geo.bottom()))
        if action == "move" and (clamped_x != global_pos.x() or clamped_y != global_pos.y()):
            QCursor.setPos(clamped_x, clamped_y)

        norm_x = (clamped_x - geo.left()) / geo.width()
        norm_y = (clamped_y - geo.top()) / geo.height()
        payload = {
            "action": action,
            "x": norm_x,
            "y": norm_y,
            "screen": self.monitor.name,
            "width": geo.width(),
            "height": geo.height(),
        }
        if button:
            payload["button"] = button
        # enqueue to avoid per-event await lag
        self.server.enqueue(json.dumps(payload))

    def _maybe_wrap_position(self, pos) -> QPoint:
        """When hitting the host hot edge, wrap to the client edge to keep motion seamless."""
        if self.host_hot_edge == "none" or self.client_hot_edge == "none":
            return pos
        geo = self.geometry()
        x = pos.x()
        y = pos.y()
        host_band = max(1, self.host_hot_band)
        client_band = max(1, self.client_hot_band)

        hit = False
        if self.host_hot_edge == "right" and x >= geo.right() - host_band:
            hit = True
        elif self.host_hot_edge == "left" and x <= geo.left() + host_band:
            hit = True
        elif self.host_hot_edge == "top" and y <= geo.top() + host_band:
            hit = True
        elif self.host_hot_edge == "bottom" and y >= geo.bottom() - host_band:
            hit = True

        if hit:
            target_x = x
            target_y = y
            if self.client_hot_edge == "left":
                target_x = geo.left() + client_band
            elif self.client_hot_edge == "right":
                target_x = geo.right() - client_band
            if self.client_hot_edge == "top":
                target_y = geo.top() + client_band
            elif self.client_hot_edge == "bottom":
                target_y = geo.bottom() - client_band
            target_x = max(geo.left(), min(target_x, geo.right()))
            target_y = max(geo.top(), min(target_y, geo.bottom()))
            QCursor.setPos(target_x, target_y)
            return QPoint(target_x, target_y)
        return pos

    def _send_key_event(self, action: str, key: str, modifiers: list[str]) -> None:
        payload = {
            "action": action,
            "key": key,
            "modifiers": modifiers,
            "screen": self.monitor.name,
        }
        self.server.enqueue(json.dumps(payload))

    def _normalize_key(self, event) -> tuple[str, list[str]]:
        key_map = {
            Qt.Key_Shift: "shift",
            Qt.Key_Control: "ctrl",
            Qt.Key_Alt: "alt",
            Qt.Key_Meta: "meta",
            Qt.Key_Super_L: "meta",
            Qt.Key_Super_R: "meta",
            Qt.Key_Tab: "tab",
            Qt.Key_Backspace: "backspace",
            Qt.Key_Return: "enter",
            Qt.Key_Enter: "enter",
            Qt.Key_Escape: "esc",
            Qt.Key_Left: "left",
            Qt.Key_Right: "right",
            Qt.Key_Up: "up",
            Qt.Key_Down: "down",
            Qt.Key_Space: "space",
            Qt.Key_Home: "home",
            Qt.Key_End: "end",
            Qt.Key_PageUp: "pageup",
            Qt.Key_PageDown: "pagedown",
            Qt.Key_Delete: "delete",
            Qt.Key_Insert: "insert",
            Qt.Key_CapsLock: "capslock",
            Qt.Key_NumLock: "numlock",
            Qt.Key_ScrollLock: "scrolllock",
            Qt.Key_F1: "f1",
            Qt.Key_F2: "f2",
            Qt.Key_F3: "f3",
            Qt.Key_F4: "f4",
            Qt.Key_F5: "f5",
            Qt.Key_F6: "f6",
            Qt.Key_F7: "f7",
            Qt.Key_F8: "f8",
            Qt.Key_F9: "f9",
            Qt.Key_F10: "f10",
            Qt.Key_F11: "f11",
            Qt.Key_F12: "f12",
        }
        key_val = event.key()
        text = event.text()
        key_name = key_map.get(key_val)
        if not key_name:
            if text:
                key_name = text.lower()
            else:
                key_name = f"keycode_{int(key_val)}"
        modifiers = []
        mods = event.modifiers()
        if mods & Qt.ShiftModifier:
            modifiers.append("shift")
        if mods & Qt.ControlModifier:
            modifiers.append("ctrl")
        if mods & Qt.AltModifier:
            modifiers.append("alt")
        if mods & Qt.MetaModifier:
            modifiers.append("meta")
        return key_name, modifiers


class ControlWindow(QWidget):
    """Small control panel to pick monitor, opacity, and toggle overlay."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.monitors = list_monitors()
        self.server = MouseServer(loop=self.loop)
        self.overlay: Optional[OverlayWindow] = None
        self.overlay_enabled = False
        self.hotkey = "<ctrl>+<esc>"
        self.hotkey_listener: Optional[keyboard.GlobalHotKeys] = None
        self.host_hot_edge = "none"
        self.host_hot_velocity = 900  # pixels per second
        self.host_hot_band = 16
        self.client_hot_edge = "none"
        self.client_hot_velocity = 900
        self.client_hot_band = 16
        self._last_cursor_pos: Optional[Tuple[int, int]] = None
        self._last_cursor_time: Optional[float] = None
        self._last_trigger_time: float = 0.0
        self.cursor_timer = QTimer()
        self.cursor_timer.setInterval(30)
        self.cursor_timer.timeout.connect(self._poll_cursor)
        self.server.set_config_listener(self._on_server_config)
        self.server.set_command_listener(self._on_server_command)
        self._build_ui()
        self._start_hotkey_listener()
        self.cursor_timer.start()

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

        hot_row = QHBoxLayout()
        hot_row.addWidget(QLabel("Hot edge:"))
        self.hot_edge_combo = QComboBox()
        for edge in ["none", "left", "right", "top", "bottom"]:
            self.hot_edge_combo.addItem(edge)
        self.hot_edge_combo.setCurrentText(self.host_hot_edge)
        self.hot_edge_combo.currentTextChanged.connect(self._on_hot_edge_changed)
        hot_row.addWidget(self.hot_edge_combo)
        hot_row.addWidget(QLabel("Min speed:"))
        self.hot_speed_slider = QSlider(Qt.Horizontal)
        self.hot_speed_slider.setRange(200, 2000)
        self.hot_speed_slider.setValue(self.host_hot_velocity)
        self.hot_speed_slider.valueChanged.connect(self._on_hot_speed_changed)
        hot_row.addWidget(self.hot_speed_slider)
        self.hot_speed_label = QLabel(f"{self.host_hot_velocity}px/s")
        hot_row.addWidget(self.hot_speed_label)
        layout.addLayout(hot_row)

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

    def _on_hot_edge_changed(self, value: str) -> None:
        self.host_hot_edge = value
        self._send_config_update()

    def _on_hot_speed_changed(self, value: int) -> None:
        self.host_hot_velocity = value
        self.hot_speed_label.setText(f"{value}px/s")
        self._send_config_update()

    def _send_config_update(self) -> None:
        self.server.set_hot_config(self._current_hot_config())

    def _current_hot_config(self) -> dict:
        return {
            "hot_edge": self.host_hot_edge,
            "hot_velocity": self.host_hot_velocity,
            "hot_band": self.host_hot_band,
        }

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
            monitor=monitor,
            server=self.server,
            loop=self.loop,
            opacity=opacity_value,
            host_hot_edge=self.host_hot_edge,
            host_hot_band=self.host_hot_band,
            client_hot_edge=self.client_hot_edge,
            client_hot_band=self.client_hot_band,
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
        if self.cursor_timer.isActive():
            self.cursor_timer.stop()
        self.loop.call_soon(self.loop.stop)
        super().closeEvent(event)

    def _on_server_config(self, config: dict) -> None:
        host_cfg = config.get("host", {})
        client_cfg = config.get("client", {})

        edge = host_cfg.get("hot_edge", self.host_hot_edge)
        velocity = int(host_cfg.get("hot_velocity", self.host_hot_velocity))
        self.host_hot_band = int(host_cfg.get("hot_band", self.host_hot_band))
        if edge != self.host_hot_edge:
            self.host_hot_edge = edge
            self.hot_edge_combo.blockSignals(True)
            self.hot_edge_combo.setCurrentText(edge)
            self.hot_edge_combo.blockSignals(False)
        if velocity != self.host_hot_velocity:
            self.host_hot_velocity = velocity
            self.hot_speed_slider.blockSignals(True)
            self.hot_speed_slider.setValue(velocity)
            self.hot_speed_slider.blockSignals(False)
            self.hot_speed_label.setText(f"{velocity}px/s")

        self.client_hot_edge = client_cfg.get("hot_edge", self.client_hot_edge)
        self.client_hot_velocity = int(client_cfg.get("hot_velocity", self.client_hot_velocity))
        self.client_hot_band = int(client_cfg.get("hot_band", self.client_hot_band))
        if self.overlay:
            self.overlay.update_hot_config(
                host_hot_edge=self.host_hot_edge,
                host_hot_band=self.host_hot_band,
                client_hot_edge=self.client_hot_edge,
                client_hot_band=self.client_hot_band,
            )

    def _on_server_command(self, command: str, payload: dict) -> None:
        if command == "hot_edge_hit" and self.overlay_enabled:
            # When client signals a return, place the cursor at this host's edge
            monitor = self.monitors[self.monitor_combo.currentIndex()]
            rect = monitor.rect()
            x = rect.center().x()
            y = rect.center().y()
            band = max(1, self.host_hot_band)
            if self.host_hot_edge == "left":
                x = rect.left() + band
            elif self.host_hot_edge == "right":
                x = rect.right() - band
            elif self.host_hot_edge == "top":
                y = rect.top() + band
            elif self.host_hot_edge == "bottom":
                y = rect.bottom() - band
            QCursor.setPos(x, y)
            asyncio.create_task(self.disable_overlay())

    def _poll_cursor(self) -> None:
        if self.host_hot_edge == "none":
            self._last_cursor_pos = None
            self._last_cursor_time = None
            return
        now = time.monotonic()
        pos = QCursor.pos()
        pos_tuple = (pos.x(), pos.y())
        speed = 0.0
        if self._last_cursor_pos and self._last_cursor_time:
            dx = pos_tuple[0] - self._last_cursor_pos[0]
            dy = pos_tuple[1] - self._last_cursor_pos[1]
            dt = max(1e-3, now - self._last_cursor_time)
            speed = (dx * dx + dy * dy) ** 0.5 / dt
        self._last_cursor_pos = pos_tuple
        self._last_cursor_time = now
        if self.overlay_enabled:
            return
        if speed < self.host_hot_velocity:
            return
        monitor = self.monitors[self.monitor_combo.currentIndex()]
        rect = monitor.rect()
        if self._is_in_hot_zone(pos_tuple, rect):
            if now - self._last_trigger_time > 0.6:
                self._last_trigger_time = now
                asyncio.create_task(self.enable_overlay())

    def _is_in_hot_zone(self, pos: Tuple[int, int], rect: QRect) -> bool:
        x, y = pos
        band = self.host_hot_band
        if not rect.contains(x, y):
            return False
        if self.host_hot_edge == "left":
            return x <= rect.left() + band
        if self.host_hot_edge == "right":
            return x >= rect.right() - band
        if self.host_hot_edge == "top":
            return y <= rect.top() + band
        if self.host_hot_edge == "bottom":
            return y >= rect.bottom() - band
        return False


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
