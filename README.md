## Cross Pointers

Minimal host/client mouse sharing with an always-on-top overlay and WebSocket streaming.

### Features
- Pick the monitor to share from a small control window.
- Always-on-top overlay that traps the cursor within the chosen display.
- Global hotkey `<ctrl>+<alt>+m` to toggle the overlay on/off.
- Adjustable overlay opacity via slider while running.
- WebSocket broadcast of normalized cursor positions for fast client updates.

### Requirements
- Python 3.12+
- Dependencies are managed with `uv` (already configured in `.venv`).

### Host (sharing machine)
```bash
cd /home/anupamkris/code/cross-pointers
uv run python main.py
```
- Choose the target monitor, set opacity, click **Start overlay**.
- Use `<ctrl>+<alt>+m` anytime to toggle the overlay and mouse trapping.
- The host WebSocket server listens on `ws://0.0.0.0:8765`.

### Client (viewing machine)
```bash
cd /home/anupamkris/code/cross-pointers
uv run python client.py --server ws://<host-ip>:8765 --opacity 0.25
```
- An always-on-top overlay renders the remote cursor.
- Adjust initial size with `--width` / `--height` if desired.

### Notes
- The overlay traps the mouse inside the selected monitor while active.
- If the connection drops, the client will automatically retry.

