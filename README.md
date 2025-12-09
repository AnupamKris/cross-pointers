## Cross Pointers

Minimal host/client mouse sharing with an always-on-top host overlay, UDP fast-path, and a client that directly moves the local mouse (no overlay).

### Features
- Host: pick monitor to share; overlay traps cursor inside chosen display.
- Hotkey `<ctrl>+<alt>+m` to toggle the host overlay on/off.
- Adjustable host overlay opacity via slider while running.
- Movement broadcast over WebSocket (handshake/fallback) plus lower-latency UDP.
- Client: connects, registers UDP port, and controls the local OS mouse (Windows/macOS/Linux).
- Client remembers last server/ports in `~/.cross_pointers_client.json`.

### Requirements
- Python 3.12+
- Dependencies are managed with `uv` (already configured in `.venv`).

### Host (sharing machine)
```bash
cd /home/anupamkris/code/cross-pointers
uv run python main.py
```
- Choose target monitor, set opacity, click **Start overlay**.
- Use `<ctrl>+<alt>+m` anytime to toggle the overlay and mouse trapping.
- Host WebSocket listens on `ws://0.0.0.0:8765` and will also send mouse updates via UDP to registered clients.

### Client (controlled machine)
```bash
cd /home/anupamkris/code/cross-pointers
uv run python client.py --server 192.168.x.x --port 8765 --udp-port 9876
```
- Client window collects host and port info, then moves the local cursor based on incoming events.
- UDP is used for fast updates; WebSocket stays connected for registration/fallback.
- Config persists at `~/.cross_pointers_client.json`.

### Notes
- Host overlay traps the mouse inside the selected monitor while active.
- If the connection drops, the client will automatically retry.

