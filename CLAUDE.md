# gas_monitor

Real-time Ethereum block, gas price and event monitor. Streams `newHeads` and `logs` over a raw JSON-RPC WebSocket and shows them in a Textual TUI or a PySide6 desktop GUI. Python 3.10+.

## Layout

| File | Role |
| --- | --- |
| `main.py` | entry point: loads config, wires metrics + client, picks the UI (lazy import) |
| `config.py` | `.env` / environment / CLI configuration and validation |
| `ws_client.py` | async JSON-RPC WebSocket client, subscriptions, reconnection |
| `metrics.py` | rolling window and derived statistics (`Metrics`, `Stats`) |
| `decoder.py` | keccak-256, EIP-55 addresses, event signature table, log decoding |
| `formatting.py` | UI-independent display helpers shared by the TUI and the GUI (no Textual or Qt imports) |
| `tui.py` | Textual app: status bar, gas panel, blocks table, event feed |
| `gui/` | PySide6 desktop GUI (`app.py`, `main_window.py`, `status_bar.py`, `gas_panel.py`, `blocks_model.py`, `blocks_view.py`, `events_view.py`, `theme.py`) |
| `tests/` | unittest suite: `formatting.py`, the blocks table model and scroll position, the base fee chart (GUI tests skip when the GUI packages or Qt libraries are missing) |
| `requirements.txt` | TUI dependencies only |
| `requirements-gui.txt` | GUI dependencies (includes `requirements.txt`) |

Run the TUI: `python main.py`. Run the GUI: `python main.py --gui` (or `GUI=true`). See README.md for flags and env variables.
Tests: `python -m unittest discover -s tests -t .`

## GUI rules (the GUI is done, keep these true)

- `ws_client.py`, `metrics.py` and `decoder.py` stay untouched unless the user explicitly asks. The GUI only reads from `Metrics` and uses `EthWsClient` as is.
- Shared formatting lives in `formatting.py`; `tui.py` and `gui/` both import from it. Never import Textual or Qt there.
- `main.py` imports the chosen UI lazily: `--gui` must not need Textual, and the TUI must not need PySide6.
- One thread only: `qasync` runs asyncio inside the Qt event loop. No extra threads, no locks.
- The GUI redraws from a `QTimer` (250 ms) and only when `metrics.version`, `blocks_version` or `log_seq` changed, with the same rate limits as the TUI. Never throttle data ingestion.
- The blocks table is a `QAbstractTableModel` updated incrementally (keep the scroll position). The event feed keeps at most 250 lines.
- The TUI must keep working unchanged: `python main.py` has to start after every change.

## Workflow

- Work in small steps. After each step: stop, summarize what changed in a few lines, and tell exactly how to test it by hand (command + what should be visible). Do not start the next step until the user confirms.
- Keep changes small and reviewable. Do not refactor unrelated code.
- Never commit unless asked.
- Before finishing a step, check that `python main.py` (the TUI) still starts.

## Code style

- File and folder names: lowercase.
- Code, comments, docstrings and commit messages in English. Comments and docstrings start lowercase, like the existing code.
- Type hints everywhere, `from __future__ import annotations` at the top of each module.
- Follow the style of the existing files (small functions, short docstrings, constants at the top of the module).
