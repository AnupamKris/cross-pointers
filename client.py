import argparse
import asyncio
import json
import sys
from typing import Optional

import qasync
import websockets
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class RemoteCursorOverlay(QWidget):
    """Lightweight always-on-top overlay that renders the remote cursor."""

    def __init__(self, opacity: float = 0.3) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(opacity)
        self.setMouseTracking(True)

        self.cursor_pos = QPoint(0, 0)
        self.status: str = "Connecting..."
        self.remote_size: Optional[tuple[int, int]] = None

    def set_status(self, message: str) -> None:
        self.status = message
        self.update()

    def apply_remote(self, payload: dict) -> None:
        width = payload.get("width", self.width())
        height = payload.get("height", self.height())
        self.remote_size = (width, height)

        norm_x = float(payload.get("x", 0))
        norm_y = float(payload.get("y", 0))
        self.cursor_pos = QPoint(
            int(norm_x * self.width()), int(norm_y * self.height())
        )
        screen_name = payload.get("screen", "remote")
        self.status = f"Live from {screen_name} ({width}x{height})"
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        overlay = QColor(10, 10, 10, int(255 * self.windowOpacity()))
        painter.fillRect(self.rect(), overlay)

        # Draw status text in the top-left.
        painter.setPen(QColor(200, 200, 200))
        painter.drawText(12, 24, self.status)

        # Draw the remote cursor as a bright crosshair.
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(255, 140, 0))
        pen.setWidth(3)
        painter.setPen(pen)
        painter.drawEllipse(self.cursor_pos, 10, 10)
        painter.drawLine(self.cursor_pos + QPoint(-18, 0), self.cursor_pos + QPoint(18, 0))
        painter.drawLine(self.cursor_pos + QPoint(0, -18), self.cursor_pos + QPoint(0, 18))


async def connect_and_render(uri: str, overlay: RemoteCursorOverlay) -> None:
    """Connect to the host and stream cursor events."""
    while True:
        try:
            overlay.set_status(f"Connecting to {uri} ...")
            async with websockets.connect(uri) as websocket:
                overlay.set_status(f"Connected to {uri}")
                async for message in websocket:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    overlay.apply_remote(data)
        except asyncio.CancelledError:
            overlay.set_status("Disconnected (stopped)")
            break
        except Exception as exc:
            overlay.set_status(f"Disconnected: {exc}. Reconnecting...")
            await asyncio.sleep(1.5)


class ClientWindow(QWidget):
    """Small control panel to enter server info and launch the overlay."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        initial_server: str = "ws://127.0.0.1:8765",
        initial_width: int = 960,
        initial_height: int = 540,
        initial_opacity: float = 0.25,
    ) -> None:
        super().__init__()
        self.loop = loop
        self.connection_task: Optional[asyncio.Task] = None
        self.overlay = RemoteCursorOverlay(opacity=initial_opacity)
        self.overlay.resize(initial_width, initial_height)

        self._build_ui(initial_server, initial_width, initial_height, initial_opacity)

    def _build_ui(
        self,
        initial_server: str,
        initial_width: int,
        initial_height: int,
        initial_opacity: float,
    ) -> None:
        self.setWindowTitle("Cross Pointers - Client")
        layout = QVBoxLayout()

        server_row = QHBoxLayout()
        server_row.addWidget(QLabel("Server/host:"))
        self.server_input = QLineEdit()
        host, port = self._split_server(initial_server)
        self.server_input.setText(host)
        server_row.addWidget(self.server_input)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        server_row.addWidget(QLabel("Port:"))
        server_row.addWidget(self.port_spin)
        layout.addLayout(server_row)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Width:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(100, 4000)
        self.width_spin.setValue(initial_width)
        size_row.addWidget(self.width_spin)

        size_row.addWidget(QLabel("Height:"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(100, 4000)
        self.height_spin.setValue(initial_height)
        size_row.addWidget(self.height_spin)
        layout.addLayout(size_row)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Opacity:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(initial_opacity * 100))
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self.opacity_slider)
        layout.addLayout(opacity_row)

        button_row = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self.disconnect_button.setEnabled(False)
        button_row.addWidget(self.connect_button)
        button_row.addWidget(self.disconnect_button)
        layout.addLayout(button_row)

        self.status_label = QLabel("Not connected.")
        layout.addWidget(self.status_label)

        self.setLayout(layout)
        self.resize(460, 180)

    def _split_server(self, server: str) -> tuple[str, int]:
        host_port = server
        if server.startswith(("ws://", "wss://")):
            host_port = server.split("://", 1)[1]
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                return host_port, 8765
        return host_port, 8765

    def _build_url(self) -> str:
        base = self.server_input.text().strip()
        port = self.port_spin.value()
        if not base:
            base = "127.0.0.1"
        if base.startswith(("ws://", "wss://")):
            host_part = base.split("://", 1)[1]
            if ":" not in host_part:
                return f"{base}:{port}"
            return base
        return f"ws://{base}:{port}"

    def _update_buttons(self) -> None:
        connecting = self.connection_task is not None and not self.connection_task.done()
        self.connect_button.setEnabled(not connecting)
        self.disconnect_button.setEnabled(connecting)

    def _on_opacity_changed(self, value: int) -> None:
        self.overlay.setWindowOpacity(value / 100)
        self.overlay.update()

    def _on_connect_clicked(self) -> None:
        if self.connection_task and not self.connection_task.done():
            return
        url = self._build_url()
        self.overlay.resize(self.width_spin.value(), self.height_spin.value())
        self.overlay.show()
        self.status_label.setText(f"Connecting to {url} ...")
        self.connection_task = self.loop.create_task(self._run_connection(url))
        self.connection_task.add_done_callback(lambda _: self._update_buttons())
        self._update_buttons()

    def _on_disconnect_clicked(self) -> None:
        self.loop.create_task(self._stop_connection())

    async def _stop_connection(self) -> None:
        if self.connection_task:
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass
            self.connection_task = None
        self.status_label.setText("Disconnected.")
        self.overlay.set_status("Disconnected (stopped)")
        self._update_buttons()

    async def _run_connection(self, url: str) -> None:
        try:
            await connect_and_render(url, self.overlay)
        except asyncio.CancelledError:
            raise
        finally:
            if self.connection_task and self.connection_task.done():
                self.connection_task = None
            self.status_label.setText("Disconnected.")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.loop.create_task(self._stop_connection())
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross Pointers client")
    parser.add_argument(
        "--server",
        default="ws://127.0.0.1:8765",
        help="WebSocket address of the host (default: ws://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--opacity",
        type=float,
        default=0.25,
        help="Overlay opacity between 0 and 1 (default: 0.25)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=960,
        help="Initial overlay width (client side, default 960)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=540,
        help="Initial overlay height (client side, default 540)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ClientWindow(
        loop=loop,
        initial_server=args.server,
        initial_width=args.width,
        initial_height=args.height,
        initial_opacity=args.opacity,
    )
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()

