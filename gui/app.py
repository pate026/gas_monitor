"""GUI entry point: runs asyncio inside the Qt event loop (qasync), on a single thread."""

from __future__ import annotations

import asyncio
import functools
import signal
import sys
from collections.abc import Callable, Coroutine
from contextvars import Context
from types import FrameType
from typing import Any

import qasync
from PySide6.QtWidgets import QApplication

from config import Config
from gui.main_window import MainWindow
from gui.theme import apply_dark_theme
from metrics import Metrics
from ws_client import EthWsClient

APP_NAME = "gas monitor"
SHUTDOWN_TIMEOUT = 5.0  # seconds to wait for the client to close its connection


def run_gui(config: Config, metrics: Metrics, client: EthWsClient) -> int:
    """open the main window and stream until it is closed; returns the exit code."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    apply_dark_theme(app)
    # closing the window must not stop the loop before the client has shut down
    app.setQuitOnLastWindowClosed(False)
    return _run_in_qt_loop(app, _run(config, metrics, client))


class _QEventLoop(qasync.QEventLoop):
    """qasync loop that accepts the context argument Python 3.15 passes for socket callbacks.

    qasync 0.28 does not take context= in _add_reader / _add_writer, so every TLS handshake
    fails with a TypeError; run the callback in the given context instead.
    """

    def _add_reader(
        self, fd: Any, callback: Callable[..., Any], *args: Any, context: Context | None = None
    ) -> None:
        super()._add_reader(fd, *_bind_context(callback, args, context))

    def _add_writer(
        self, fd: Any, callback: Callable[..., Any], *args: Any, context: Context | None = None
    ) -> None:
        super()._add_writer(fd, *_bind_context(callback, args, context))


def _bind_context(
    callback: Callable[..., Any], args: tuple[Any, ...], context: Context | None
) -> tuple[Any, ...]:
    if context is None:
        return (callback, *args)
    return (functools.partial(context.run, callback, *args),)


def _run_in_qt_loop(app: QApplication, coro: Coroutine[Any, Any, int]) -> int:
    if sys.version_info >= (3, 12):
        return asyncio.run(coro, loop_factory=lambda: _QEventLoop(app))
    return qasync.run(coro)  # older Pythons never pass context= to _add_reader


async def _run(config: Config, metrics: Metrics, client: EthWsClient) -> int:
    closed = asyncio.Event()
    window = MainWindow(config, metrics, client)
    window.closed.connect(closed.set)

    # Ctrl+C in the terminal closes the window like the close button does; the handler
    # runs on the next Python callback, at the latest on the window's refresh timer
    def on_sigint(_signum: int, _frame: FrameType | None) -> None:
        window.close()

    previous_handler = signal.signal(signal.SIGINT, on_sigint)
    window.show()
    client_task = asyncio.create_task(client.run(), name="ws-client")
    closed_task = asyncio.create_task(closed.wait(), name="window-closed")
    try:
        await asyncio.wait({client_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        closed_task.cancel()
        if window.isVisible():  # Ctrl+C or a client error; a user close already happened
            window.close()
        await _stop_client(client, client_task)
    return 0


async def _stop_client(client: EthWsClient, task: asyncio.Task[None]) -> None:
    """stop the client and wait for it; re-raises if it failed on its own."""
    if not task.done():
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            pass  # wait_for has cancelled the task
        return
    task.result()  # the client never returns on its own, so surface its error
