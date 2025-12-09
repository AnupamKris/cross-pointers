import argparse
import asyncio
import json
import sys
from typing import Optional

import qasync
import websockets
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


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
        except Exception as exc:
            overlay.set_status(f"Disconnected: {exc}. Reconnecting...")
            await asyncio.sleep(1.5)


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

    overlay = RemoteCursorOverlay(opacity=args.opacity)
    overlay.resize(args.width, args.height)
    overlay.show()

    asyncio.create_task(connect_and_render(args.server, overlay))

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()

