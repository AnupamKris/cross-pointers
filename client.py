import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, Union

import qasync
import websockets
from pynput.mouse import Button, Controller
from pynput.keyboard import Controller as KeyController, Key
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
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
    """Maps normalized coordinates to the local screen and drives the OS cursor + keyboard."""

    def __init__(self) -> None:
        self.mouse = Controller()
        self.keyboard = KeyController()

    def apply_normalized(self, norm_x: float, norm_y: float) -> None:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.geometry()
        x = max(geo.left(), min(geo.right(), int(geo.left() + norm_x * geo.width())))
        y = max(geo.top(), min(geo.bottom(), int(geo.top() + norm_y * geo.height())))
        self.mouse.position = (x, y)

    def click_action(self, action: str, button_name: str) -> None:
        btn = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
            "x1": Button.x1,
            "x2": Button.x2,
        }.get(button_name, Button.left)
        if action == "down":
            self.mouse.press(btn)
        elif action == "up":
            self.mouse.release(btn)

    def key_action(self, action: str, key_name: str) -> None:
        key_obj = self._key_from_name(key_name)
        if key_obj is None:
            return
        try:
            if action == "key_down":
                self.keyboard.press(key_obj)
            elif action == "key_up":
                self.keyboard.release(key_obj)
        except Exception:
            # Ignore platform-specific failures
            pass

    def _key_from_name(self, name: str):
        special = {
            "shift": Key.shift,
            "ctrl": Key.ctrl,
            "alt": Key.alt,
            "meta": Key.cmd,
            "cmd": Key.cmd,
            "tab": Key.tab,
            "backspace": Key.backspace,
            "enter": Key.enter,
            "esc": Key.esc,
            "left": Key.left,
            "right": Key.right,
            "up": Key.up,
            "down": Key.down,
            "space": Key.space,
            "home": Key.home,
            "end": Key.end,
            "pageup": Key.page_up,
            "pagedown": Key.page_down,
            "delete": Key.delete,
            "insert": Key.insert,
            "capslock": Key.caps_lock,
            "numlock": Key.num_lock,
            "scrolllock": Key.scroll_lock,
            "f1": Key.f1,
            "f2": Key.f2,
            "f3": Key.f3,
            "f4": Key.f4,
            "f5": Key.f5,
            "f6": Key.f6,
            "f7": Key.f7,
            "f8": Key.f8,
            "f9": Key.f9,
            "f10": Key.f10,
            "f11": Key.f11,
            "f12": Key.f12,
        }
        if name in special:
            return special[name]
        if len(name) == 1:
            return name
        return None


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
    screen = payload.get("screen", "remote")
    if action in {"move", "down", "up"}:
        norm_x = float(payload.get("x", 0.0))
        norm_y = float(payload.get("y", 0.0))
        if action == "move":
            applier.apply_normalized(norm_x, norm_y)
        elif action in {"down", "up"}:
            button = payload.get("button", "left")
            applier.click_action(action, button)
    elif action in {"key_down", "key_up"}:
        key_name = payload.get("key", "")
        # Ignore our host hotkey combination (handled host-side already)
        if not (key_name == "esc" and "ctrl" in payload.get("modifiers", [])):
            applier.key_action(action, key_name)
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


async def connect_and_control(
    uri: str,
    udp_port: int,
    applier: MouseApplier,
    status_cb,
    config: dict,
    outbound_queue: Optional[asyncio.Queue[str]] = None,
    on_config=None,
) -> None:
    """WebSocket handshake for registration + fallback data channel."""
    while True:
        try:
            status_cb(f"Connecting to {uri} ...")
            async with websockets.connect(uri) as websocket:
                await websocket.send(
                    json.dumps(
                        {"type": "hello", "udp_port": udp_port, "config": config}
                    )
                )
                status_cb(f"Connected to {uri} (UDP {udp_port})")
                consumer = asyncio.create_task(_ws_consumer(websocket, applier, status_cb, on_config))
                producer = (
                    asyncio.create_task(_ws_producer(websocket, outbound_queue))
                    if outbound_queue is not None
                    else None
                )
                tasks = {consumer}
                if producer:
                    tasks.add(producer)
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except asyncio.CancelledError:
            status_cb("Disconnected (stopped)")
            break
        except Exception as exc:
            status_cb(f"Disconnected: {exc}. Reconnecting...")
            await asyncio.sleep(1.5)


async def _ws_consumer(websocket, applier: MouseApplier, status_cb, on_config) -> None:
    async for message in websocket:
        parsed = parse_payload(message)
        if not parsed:
            continue
        if parsed.get("type") == "config_update":
            config = parsed.get("config", {})
            if on_config:
                on_config(config)
            continue
        _handle_parsed(parsed, applier, status_cb)


async def _ws_producer(websocket, queue: asyncio.Queue[str]) -> None:
    while True:
        msg = await queue.get()
        try:
            await websocket.send(msg)
        finally:
            queue.task_done()


class ClientWindow(QWidget):
    """Control panel that connects and moves the local mouse; no overlay."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        initial_server: str,
        initial_port: int,
        initial_udp_port: int,
        initial_hot_edge: str,
        initial_hot_velocity: int,
        initial_hot_band: int,
    ) -> None:
        super().__init__()
        self.loop = loop
        self.connection_task: Optional[asyncio.Task] = None
        self.udp_task: Optional[asyncio.Task] = None
        self.applier = MouseApplier()
        self.hot_edge = initial_hot_edge
        self.hot_velocity = initial_hot_velocity
        self.hot_band = initial_hot_band
        self.outbound_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._last_cursor_pos: Optional[Tuple[int, int]] = None
        self._last_cursor_time: Optional[float] = None
        self._last_hit_time: float = 0.0
        self.cursor_timer = QTimer()
        self.cursor_timer.setInterval(30)
        self.cursor_timer.timeout.connect(self._poll_cursor)
        self._build_ui(initial_server, initial_port, initial_udp_port)
        self._update_buttons()
        self.cursor_timer.start()

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

        hot_row = QHBoxLayout()
        hot_row.addWidget(QLabel("Hot edge:"))
        self.hot_edge_combo = QComboBox()
        for edge in ["none", "left", "right", "top", "bottom"]:
            self.hot_edge_combo.addItem(edge)
        self.hot_edge_combo.setCurrentText(self.hot_edge)
        self.hot_edge_combo.currentTextChanged.connect(self._on_hot_edge_changed)
        hot_row.addWidget(self.hot_edge_combo)
        hot_row.addWidget(QLabel("Min speed:"))
        self.hot_speed_slider = QSlider(Qt.Horizontal)
        self.hot_speed_slider.setRange(200, 2000)
        self.hot_speed_slider.setValue(self.hot_velocity)
        self.hot_speed_slider.valueChanged.connect(self._on_hot_speed_changed)
        hot_row.addWidget(self.hot_speed_slider)
        self.hot_speed_label = QLabel(f"{self.hot_velocity}px/s")
        hot_row.addWidget(self.hot_speed_label)
        layout.addLayout(hot_row)

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

    def _on_hot_edge_changed(self, value: str) -> None:
        self.hot_edge = value
        self._send_config_update()

    def _on_hot_speed_changed(self, value: int) -> None:
        self.hot_velocity = value
        self.hot_speed_label.setText(f"{value}px/s")
        self._send_config_update()

    def _current_config(self) -> dict:
        return {
            "server": self.server_input.text().strip(),
            "port": self.port_spin.value(),
            "udp_port": self.udp_spin.value(),
            **self._current_hot_config(),
        }

    def _current_hot_config(self) -> dict:
        return {
            "hot_edge": self.hot_edge,
            "hot_velocity": self.hot_velocity,
            "hot_band": self.hot_band,
        }

    def _save_current_config(self) -> None:
        save_config(
            self._current_config()
        )

    def _send_config_update(self) -> None:
        payload = json.dumps(
            {"type": "config_update", "config": self._current_hot_config()}
        )
        try:
            self.outbound_queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass
        self._save_current_config()

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
                config=self._current_hot_config(),
                outbound_queue=self.outbound_queue,
                on_config=self._apply_remote_config,
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

    def _apply_remote_config(self, config: dict) -> None:
        edge = config.get("hot_edge", self.hot_edge)
        velocity = int(config.get("hot_velocity", self.hot_velocity))
        band = int(config.get("hot_band", self.hot_band))
        changed = False
        if edge != self.hot_edge:
            self.hot_edge = edge
            self.hot_edge_combo.blockSignals(True)
            self.hot_edge_combo.setCurrentText(edge)
            self.hot_edge_combo.blockSignals(False)
            changed = True
        if velocity != self.hot_velocity:
            self.hot_velocity = velocity
            self.hot_speed_slider.blockSignals(True)
            self.hot_speed_slider.setValue(velocity)
            self.hot_speed_slider.blockSignals(False)
            self.hot_speed_label.setText(f"{velocity}px/s")
            changed = True
        if band != self.hot_band:
            self.hot_band = band
            changed = True
        if changed:
            self._save_current_config()

    def _poll_cursor(self) -> None:
        if self.hot_edge == "none":
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
        if speed < self.hot_velocity:
            return
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        rect = screen.geometry()
        if self._is_in_hot_zone(pos_tuple, rect):
            if now - self._last_hit_time > 0.6:
                self._last_hit_time = now
                self._emit_hot_edge_hit()

    def _is_in_hot_zone(self, pos: Tuple[int, int], rect) -> bool:
        x, y = pos
        band = self.hot_band
        if not rect.contains(x, y):
            return False
        if self.hot_edge == "left":
            return x <= rect.left() + band
        if self.hot_edge == "right":
            return x >= rect.right() - band
        if self.hot_edge == "top":
            return y <= rect.top() + band
        if self.hot_edge == "bottom":
            return y >= rect.bottom() - band
        return False

    def _emit_hot_edge_hit(self) -> None:
        if self.connection_task is None or self.connection_task.done():
            return
        payload = json.dumps({"type": "hot_edge_hit", "edge": self.hot_edge})
        try:
            self.outbound_queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.cursor_timer.isActive():
            self.cursor_timer.stop()
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
    parser.add_argument(
        "--hot-edge",
        default=config.get("hot_edge", "none"),
        choices=["none", "left", "right", "top", "bottom"],
        help="Edge that will toggle overlay (default: none)",
    )
    parser.add_argument(
        "--hot-velocity",
        type=int,
        default=int(config.get("hot_velocity", 900)),
        help="Minimum px/s to trigger hot edge (default: 900)",
    )
    parser.add_argument(
        "--hot-band",
        type=int,
        default=int(config.get("hot_band", 16)),
        help="Hit box width in pixels (default: 16)",
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
        initial_hot_edge=args.hot_edge,
        initial_hot_velocity=args.hot_velocity,
        initial_hot_band=args.hot_band,
    )
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()

