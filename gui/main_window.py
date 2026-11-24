"""main window: layout, controls (pause / clear / quit) and the refresh timer that reads Metrics."""

from __future__ import annotations

import time

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import QFrame, QMainWindow, QSplitter, QToolBar, QVBoxLayout

from config import Config
from formatting import chain_label
from gui.blocks_view import BlocksPanel
from gui.events_view import EventsPanel
from gui.gas_panel import GasPanel
from gui.status_bar import StatusBar
from gui.theme import PAUSED_COLOR
from metrics import Metrics
from ws_client import EthWsClient

REFRESH_INTERVAL_MS = 250  # status bar tick; ingestion itself is never throttled
# on busy chains data changes on every tick, so the costlier widgets are rate-limited
# (same intervals as the TUI); when data is quiet the next change shows on the next tick
GAS_MIN_INTERVAL = 0.5
GAS_IDLE_INTERVAL = 1.0  # redraw the gas cards at least this often (the event rate decays)
CHART_MIN_INTERVAL = 1.0
TABLE_MIN_INTERVAL = 1.0
TABLE_EVENTS_INTERVAL = 2.5  # when only event counts changed (logs stream in for seconds)
FEED_MIN_INTERVAL = 0.5
DEFAULT_SIZE = (1280, 760)
TOP_SPLIT = (420, 860)  # initial widths of the gas panel and the blocks table
MAIN_SPLIT = (500, 260)  # initial heights of the top row and the event feed
FRAME_WIDTH = 2  # the window frame turns yellow while paused
APP_TITLE = "gas monitor"
# window size and splitter positions survive restarts (~/.config/gas_monitor/gui.conf on Linux)
SETTINGS_ORGANIZATION = "gas_monitor"
SETTINGS_APPLICATION = "gui"
GEOMETRY_KEY = "window/geometry"
TOP_SPLIT_KEY = "window/top_split"
MAIN_SPLIT_KEY = "window/main_split"


class MainWindow(QMainWindow):
    """gas monitor window; only reads from metrics and the client's status."""

    closed = Signal()  # emitted when the user closes the window

    def __init__(self, config: Config, metrics: Metrics, client: EthWsClient) -> None:
        super().__init__()
        self.config = config
        self.metrics = metrics
        self.client = client
        self._gas_version = -1
        self._gas_drawn_at = 0.0
        self._chart_version = -1
        self._chart_drawn_at = 0.0
        self._table_state: tuple[int, int, bool] | None = None  # metrics state the table shows
        self._table_drawn_at = 0.0
        self._feed_drawn_at = 0.0
        self.paused = False
        self.setWindowTitle(APP_TITLE)
        self.resize(*DEFAULT_SIZE)

        self._status = StatusBar()
        self._gas = GasPanel(metrics)
        self._blocks = BlocksPanel(metrics)
        self._events = EventsPanel(config, metrics)
        self._top_split = top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self._gas)
        top.addWidget(self._blocks)
        top.setSizes(list(TOP_SPLIT))
        self._main_split = main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(self._events)
        main.setSizes(list(MAIN_SPLIT))

        self._frame = QFrame()
        self._frame.setObjectName("frame")
        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._status)
        layout.addWidget(main, 1)
        self.setCentralWidget(self._frame)
        self._set_frame_color(None)
        self._pause_action = self._add_actions()
        self._restore_layout()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.update_view)
        self._timer.start()
        self.update_view()

    def _add_actions(self) -> QAction:
        """toolbar buttons with the TUI's keys; the toolbar doubles as its key help footer."""
        toolbar = QToolBar("controls")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)
        pause = QAction("Pause (P)", self)
        pause.setCheckable(True)
        pause.setShortcut(QKeySequence("P"))
        pause.toggled.connect(self.set_paused)
        clear = QAction("Clear logs (C)", self)
        clear.setShortcut(QKeySequence("C"))
        clear.triggered.connect(self.clear_logs)
        quit_ = QAction("Quit (Q)", self)
        quit_.setShortcuts([QKeySequence("Q"), QKeySequence(QKeySequence.StandardKey.Quit)])
        quit_.triggered.connect(self.close)
        for action in (pause, clear, quit_):
            # window-wide, so the keys also work while the feed or the table has focus
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            toolbar.addAction(action)
        return pause

    # --- layout persistence --------------------------------------------------------

    def _restore_layout(self) -> None:
        """reapply the last session's size and splitters; Wayland ignores the position."""
        settings = _settings()
        for key, restore in (
            (GEOMETRY_KEY, self.restoreGeometry),
            (TOP_SPLIT_KEY, self._top_split.restoreState),
            (MAIN_SPLIT_KEY, self._main_split.restoreState),
        ):
            value = settings.value(key)
            if isinstance(value, QByteArray):
                restore(value)  # returns False for unusable data, keeping the defaults

    def _save_layout(self) -> None:
        settings = _settings()
        settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(TOP_SPLIT_KEY, self._top_split.saveState())
        settings.setValue(MAIN_SPLIT_KEY, self._main_split.saveState())

    # --- actions -------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """stop redrawing the panels; data keeps flowing into metrics meanwhile."""
        if paused == self.paused:
            return
        self.paused = paused
        self._pause_action.setChecked(paused)
        self._pause_action.setText("Resume (P)" if paused else "Pause (P)")
        self._set_frame_color(PAUSED_COLOR if paused else None)
        # catch up immediately on resume
        self._gas_drawn_at = self._chart_drawn_at = self._table_drawn_at = self._feed_drawn_at = 0.0
        self.update_view()

    def clear_logs(self) -> None:
        self._events.clear()

    def _set_frame_color(self, color: str | None) -> None:
        border = color or "transparent"
        self._frame.setStyleSheet(f"#frame {{ border: {FRAME_WIDTH}px solid {border}; }}")

    # --- rendering -----------------------------------------------------------------

    def update_view(self) -> None:
        """redraw from the current metrics; runs on a timer, independent of data arrival."""
        now = time.monotonic()
        stats = self.metrics.stats()
        status = self.client.status
        self._status.render(status, self.metrics, stats, now, paused=self.paused)
        title = APP_TITLE if status.chain_id is None else f"{APP_TITLE} · {chain_label(status.chain_id)}"
        if self.windowTitle() != title:
            self.setWindowTitle(title)
        if self.paused:
            return
        version = self.metrics.version
        since_gas = now - self._gas_drawn_at
        if since_gas >= GAS_IDLE_INTERVAL or (
            since_gas >= GAS_MIN_INTERVAL and version != self._gas_version
        ):
            self._gas.render_stats(stats, self.client.status)
            self._gas_version, self._gas_drawn_at = version, now
        # the chart only depends on blocks, so log traffic alone never redraws it
        blocks_version = self.metrics.blocks_version
        if blocks_version != self._chart_version and now - self._chart_drawn_at >= CHART_MIN_INTERVAL:
            self._gas.render_chart()
            self._chart_version, self._chart_drawn_at = blocks_version, now
        # the busy flag is part of the state so "…" (pending) cells clear once a stream goes quiet
        table_state = (blocks_version, self.metrics.log_seq, self.metrics.log_stream_busy())
        if table_state != self._table_state:
            events_only = self._table_state is not None and table_state[0] == self._table_state[0]
            interval = TABLE_EVENTS_INTERVAL if events_only else TABLE_MIN_INTERVAL
            if now - self._table_drawn_at >= interval:
                self._blocks.render()
                self._table_state, self._table_drawn_at = table_state, now
        if now - self._feed_drawn_at >= FEED_MIN_INTERVAL:
            self._events.render(self.client.status)
            self._feed_drawn_at = now

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._timer.stop()
        self._save_layout()
        event.accept()
        self.closed.emit()


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)
