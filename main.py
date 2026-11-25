"""entry point: load the configuration, wire the components together and start the TUI or GUI."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from config import Config, ConfigError, load_config
from metrics import Metrics
from ws_client import EthWsClient

GUI_PACKAGES = {"PySide6", "shiboken6", "pyqtgraph", "qasync", "numpy"}


def _setup_logging(log_file: Path | None) -> None:
    root = logging.getLogger()
    if log_file is None:
        # a handler is still needed, otherwise Python's last-resort handler writes
        # warnings to stderr, underneath the TUI
        root.addHandler(logging.NullHandler())
        return
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _from_gui_package(exc: ImportError) -> bool:
    """tell whether an import error comes from one of the GUI packages."""
    if (exc.name or "").partition(".")[0] in GUI_PACKAGES:
        return True
    # a native extension that fails to load (e.g. a missing libEGL.so.1) only sets
    # the short module name, so look at the path of the extension file instead
    if exc.path is not None and not GUI_PACKAGES.isdisjoint(Path(exc.path).parts):
        return True
    # some packages raise a bare ImportError themselves (qasync: "No Qt implementations
    # found"), so look at the innermost frame outside the import machinery
    filename = ""
    tb = exc.__traceback__
    while tb is not None:
        if not tb.tb_frame.f_code.co_filename.startswith("<frozen"):
            filename = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    return not GUI_PACKAGES.isdisjoint(Path(filename).parts)


def _run_ui(config: Config, metrics: Metrics, client: EthWsClient) -> int:
    # imported lazily so the GUI does not need Textual and the TUI does not need PySide6
    if config.gui:
        try:
            from gui.app import run_gui
        except ImportError as exc:
            if not _from_gui_package(exc):
                raise
            if isinstance(exc, ModuleNotFoundError):
                print(f"error: the GUI needs {exc.name}: pip install -r requirements-gui.txt", file=sys.stderr)
            else:
                print(f"error: the GUI could not load Qt: {exc}", file=sys.stderr)
            return 2
        return run_gui(config, metrics, client)
    from tui import GasMonitorApp

    GasMonitorApp(config, metrics, client).run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_config(argv)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        _setup_logging(config.log_file)
    except OSError as exc:
        print(f"error: cannot open log file: {exc}", file=sys.stderr)
        return 2

    metrics = Metrics(window=config.window)
    client = EthWsClient(
        config.ws_url,
        metrics,
        log_addresses=config.contract_addresses,
        subscribe_logs=config.subscribe_logs,
        backfill_blocks=config.window,
        stale_timeout=config.stale_timeout,
    )
    return _run_ui(config, metrics, client)


if __name__ == "__main__":
    sys.exit(main())
