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
| `tests/` | unittest suite for `formatting.py` and the blocks table model |

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

## Current task: follow-up fixes from the GUI review

Small fixes found in a code review of the finished GUI. Work one phase at a time.

### Phase A: friendly error when Qt cannot load
- In `main.py` `_run_ui`, only `ModuleNotFoundError` is caught. A missing system library (e.g. `libEGL.so.1`) raises plain `ImportError` and prints a full traceback.
- Catch `ImportError` for the GUI import. Keep the current "pip install" hint when the missing module is one of `GUI_PACKAGES`; otherwise print a short message that includes the original error text (e.g. `error: the GUI could not load Qt: <exc>`) and return 2. Do not swallow errors from unrelated modules silently.
- Test by hand: `python main.py --gui` still opens the window; the TUI path is unchanged.

### Phase B: optional GUI requirements
- Split the requirements: `requirements.txt` keeps only the TUI dependencies (`websockets`, `textual`, `python-dotenv`). New `requirements-gui.txt` starts with `-r requirements.txt` and holds the GUI pins (`PySide6-Essentials`, `pyqtgraph`, `qasync`, with their existing comments and versions).
- Update the hint in `main.py` to `pip install -r requirements-gui.txt`, and the README setup / GUI sections and project layout table.
- Test by hand: a fresh venv with only `requirements.txt` runs the TUI; `python main.py --gui` there prints the hint instead of a traceback.

### Phase C: chart with a single block
- `gui/gas_panel.py` `BaseFeeChart.render` calls `setXRange(0, max(xs))` which is a zero-width range when only one block is known (the first second after connecting). Handle it (e.g. skip the call or use a small fixed range when `max(xs) == 0`).
- Add a unit test if it can be done without a display; otherwise just describe how to check it.

### Phase D: scroll position after a gap fill (optional)
- `row_diff` returns `None` when a gap is filled (backfill after a long reconnect), which resets the model and sends a scrolled-down reader back to the top.
- Only do this if a small, safe fix exists (e.g. restore the scroll value after the reset). If it needs a larger change, stop and ask the user whether to skip it.

### Phase E: wrap-up
- Run the tests and check that the TUI still starts.
- Do not commit. `CLAUDE.md` and `gui_plan.md` are still untracked; ask the user whether they should be committed.

## Workflow

- One phase at a time. After each phase: stop, summarize what changed in a few lines, and tell exactly how to test it by hand (command + what should be visible). Do not start the next phase until the user confirms.
- Keep changes small and reviewable. Do not refactor unrelated code.
- Never commit unless asked.
- Before finishing a phase, check that `python main.py` (the TUI) still starts.

## Code style

- File and folder names: lowercase.
- Code, comments, docstrings and commit messages in English. Comments and docstrings start lowercase, like the existing code.
- Type hints everywhere, `from __future__ import annotations` at the top of each module.
- Follow the style of the existing files (small functions, short docstrings, constants at the top of the module).
