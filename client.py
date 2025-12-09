import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple, Union

import qasync
import websockets
from pynput.mouse import Button, Controller
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

CONFIG_PATH = Path(os.path.expanduser("~/.cross_pointers_client.json"))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


class MouseApplier:
    """Maps normalized coordinates to the local screen and drives the OS cursor."""

    def __init__(self) -> None:
        self.controller = Controller()

    def apply_normalized(self, norm_x: float, norm_y: float) -> None:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.geometry()
        x = max(geo.left(), min(geo.right(), int(geo.left() + norm_x * geo.width())))
        y = max(geo.top(), min(geo.bottom(), int(geo.top() + norm_y * geo.height())))
        self.controller.position = (x, y)

    def click_action(self, action: str, button_name: str) -> None:
        btn = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
            "x1": Button.x1,
            "x2": Button.x2,
        }.get(button_name, Button.left)
        if action == "down":
            self.controller.press(btn)
        elif action == "up":
            self.controller.release(btn)


def parse_payload(payload: Union[bytes, str]) -> Optional[dict]:
    try:
        if isinstance(payload, bytes):
            data = json.loads(payload.decode())
        else:
            data = json.loads(payload)
        return data
    except Exception:
        return None


def _handle_parsed(payload: dict, applier: MouseApplier, status_cb, udp: bool = False) -> None:
    action = payload.get("action", "move")
    norm_x = float(payload.get("x", 0.0))
    norm_y = float(payload.get("y", 0.0))
    screen = payload.get("screen", "remote")
    if action == "move":
        applier.apply_normalized(norm_x, norm_y)
    elif action in {"down", "up"}:
        button = payload.get("button", "left")
        applier.click_action(action, button)
    if udp:
        status_cb(f"Controlling from {screen} (UDP)")


async def udp_consumer(port: int, handler) -> None:
    """Receive mouse updates over UDP for lower latency."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    class UdpProtocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    transport, _ = await loop.create_datagram_endpoint(
        UdpProtocol, local_addr=("0.0.0.0", port)
    )
    try:
        while True:
            payload = await queue.get()
            handler(payload)
    except asyncio.CancelledError:
        transport.close()
        raise


async def connect_and_control(uri: str, udp_port: int, applier: MouseApplier, status_cb) -> None:
    """WebSocket handshake for registration + fallback data channel."""
    while True:
        try:
            status_cb(f"Connecting to {uri} ...")
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({"udp_port": udp_port}))
                status_cb(f"Connected to {uri} (UDP {udp_port})")
                async for message in websocket:
                    parsed = parse_payload(message)
                    if parsed:
                        _handle_parsed(parsed, applier, status_cb)
        except asyncio.CancelledError:
            status_cb("Disconnected (stopped)")
            break
        except Exception as exc:
            status_cb(f"Disconnected: {exc}. Reconnecting...")
            await asyncio.sleep(1.5)


class ClientWindow(QWidget):
    """Control panel that connects and moves the local mouse; no overlay."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        initial_server: str,
        initial_port: int,
        initial_udp_port: int,
    ) -> None:
        super().__init__()
        self.loop = loop
        self.connection_task: Optional[asyncio.Task] = None
        self.udp_task: Optional[asyncio.Task] = None
        self.applier = MouseApplier()
        self._build_ui(initial_server, initial_port, initial_udp_port)
        self._update_buttons()

    def _build_ui(
        self,
        initial_server: str,
        initial_port: int,
        initial_udp_port: int,
    ) -> None:
        self.setWindowTitle("Cross Pointers - Client (mouse control)")
        layout = QVBoxLayout()

        server_row = QHBoxLayout()
        server_row.addWidget(QLabel("Server/host:"))
        self.server_input = QLineEdit(initial_server)
        server_row.addWidget(self.server_input)
        layout.addLayout(server_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("WebSocket port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(initial_port)
        port_row.addWidget(self.port_spin)
        port_row.addWidget(QLabel("UDP port:"))
        self.udp_spin = QSpinBox()
        self.udp_spin.setRange(1024, 65535)
        self.udp_spin.setValue(initial_udp_port)
        port_row.addWidget(self.udp_spin)
        layout.addLayout(port_row)

        button_row = QHBoxLayout()
        self.connect_button = QPushButton("Connect & control")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._on_disconnect_clicked)
        button_row.addWidget(self.connect_button)
        button_row.addWidget(self.disconnect_button)
        layout.addLayout(button_row)

        self.status_label = QLabel("Not connected.")
        layout.addWidget(self.status_label)

        self.setLayout(layout)
        self.resize(480, 160)

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

    def _save_current_config(self) -> None:
        save_config(
            {
                "server": self.server_input.text().strip(),
                "port": self.port_spin.value(),
                "udp_port": self.udp_spin.value(),
            }
        )

    def _on_connect_clicked(self) -> None:
        if self.connection_task and not self.connection_task.done():
            return
        url = self._build_url()
        udp_port = self.udp_spin.value()
        self.status_label.setText(f"Connecting to {url} (UDP {udp_port}) ...")
        self.connection_task = self.loop.create_task(
            self._run_connection(url, udp_port)
        )
        self.connection_task.add_done_callback(lambda _: self._update_buttons())
        self._update_buttons()
        self._save_current_config()

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
        if self.udp_task:
            self.udp_task.cancel()
            try:
                await self.udp_task
            except asyncio.CancelledError:
                pass
            self.udp_task = None
        self.status_label.setText("Disconnected.")
        self._update_buttons()

    async def _run_connection(self, url: str, udp_port: int) -> None:
        # Start UDP listener first for low-latency data
        if self.udp_task is None or self.udp_task.done():
            self.udp_task = self.loop.create_task(
                udp_consumer(
                    udp_port, lambda payload: self._handle_payload(payload, udp=True)
                )
            )

        try:
            await connect_and_control(
                uri=url,
                udp_port=udp_port,
                applier=self.applier,
                status_cb=self.status_label.setText,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self.connection_task and self.connection_task.done():
                self.connection_task = None
            self.status_label.setText("Disconnected.")

    def _handle_payload(self, payload: bytes, udp: bool = False) -> None:
        parsed = parse_payload(payload)
        if parsed:
            _handle_parsed(parsed, self.applier, self.status_label.setText, udp=udp)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.loop.create_task(self._stop_connection())
        super().closeEvent(event)


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="Cross Pointers client")
    parser.add_argument(
        "--server",
        default=config.get("server", "127.0.0.1"),
        help="Host/IP of the server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(config.get("port", 8765)),
        help="WebSocket port of the host (default: 8765)",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=int(config.get("udp_port", 9876)),
        help="UDP port to receive fast mouse updates (default: 9876)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ClientWindow(
        loop=loop,
        initial_server=args.server,
        initial_port=args.port,
        initial_udp_port=args.udp_port,
    )
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()

