"""gas & network panel: stat cards and the base fee history chart."""

from __future__ import annotations

from html import escape

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from formatting import (
    format_count,
    format_gwei,
    format_percent,
    format_rate,
    format_seconds,
    trend_summary,
)
from gui.theme import ACCENT_COLOR, dim_color, level_color, palette_color
from metrics import WEI_PER_GWEI, Metrics, Stats
from ws_client import Block, ConnectionStatus, LogsState

CARD_COLUMNS = 2
# (key, title); the order is the reading order, row by row
CARDS = (
    ("base_fee", "base fee"),
    ("trend", "trend"),
    ("min_avg_max", "min / avg / max"),
    ("gas_used", "gas used"),
    ("block_time", "block time"),
    ("throughput", "throughput"),
    ("events", "events"),
    ("window", "window"),
)
FEE_CARDS = ("trend", "min_avg_max")  # hidden on chains without an EIP-1559 base fee
VALUE_POINT_SIZE_DELTA = 3  # card values are this much larger than the base font
# keep at least this relative y range so near-constant fees (e.g. L2 floors) are not
# blown up into noise; the TUI flattens its sparkline for the same reason
CHART_MIN_RELATIVE_RANGE = 0.02
# x range used while only one block is known, so the range never has zero width
CHART_MIN_X_RANGE = 1
CHART_LINE_WIDTH = 2
CHART_MIN_HEIGHT = 140


class StatCard(QFrame):
    """small titled box with one value."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title = QLabel(title)
        self._title.setStyleSheet(f"color: {dim_color()};")
        self._value = QLabel()
        self._value.setTextFormat(Qt.TextFormat.RichText)
        font = QFont(self._value.font())
        font.setPointSize(font.pointSize() + VALUE_POINT_SIZE_DELTA)
        self._value.setFont(font)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addWidget(self._value)

    def set_html(self, html: str) -> None:
        # setText always repaints, so skip it when the text did not change
        if self._value.text() != html:
            self._value.setText(html)


class BaseFeeChart(pg.PlotWidget):
    """base fee per block in Gwei; x is how many blocks before the latest one."""

    def __init__(self) -> None:
        super().__init__(background=palette_color("base"))
        self.setMinimumHeight(CHART_MIN_HEIGHT)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        plot = self.getPlotItem()
        plot.showGrid(x=True, y=True, alpha=0.15)
        text = palette_color("text")
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setTextPen(text)
            axis.setPen(dim_color())
            axis.enableAutoSIPrefix(False)
        plot.setLabel("left", "Gwei", color=text)
        plot.setLabel("bottom", "blocks ago", color=text)
        plot.invertX(True)  # the latest block on the right, like the TUI sparkline
        self._curve = plot.plot(pen=pg.mkPen(ACCENT_COLOR, width=CHART_LINE_WIDTH))

    def render(self, blocks: list[Block]) -> None:
        """draw the window's base fees; blocks are newest first, as Metrics returns them."""
        points = [
            (block.number, block.base_fee / WEI_PER_GWEI)
            for block in reversed(blocks)
            if block.base_fee is not None
        ]
        if not points:
            self._curve.setData([], [])
            return
        latest = points[-1][0]
        xs = [latest - number for number, _ in points]
        fees = [fee for _, fee in points]
        self._curve.setData(xs, fees, connect="finite")
        low, high = min(fees), max(fees)
        pad = max((high - low) * 0.1, high * CHART_MIN_RELATIVE_RANGE / 2)
        self.setYRange(max(low - pad, 0.0), high + pad, padding=0)
        self.setXRange(0, max(max(xs), CHART_MIN_X_RANGE), padding=0.02)


class GasPanel(QGroupBox):
    """gas & network statistics for the rolling window."""

    def __init__(self, metrics: Metrics) -> None:
        super().__init__("gas && network")  # && is a literal ampersand in Qt titles
        self.metrics = metrics
        self._waiting = QLabel("waiting for blocks…")
        self._waiting.setStyleSheet(f"color: {dim_color()}; font-style: italic;")
        self._cards = {key: StatCard(title) for key, title in CARDS}
        grid = QGridLayout()
        grid.setSpacing(6)
        for index, (key, _) in enumerate(CARDS):
            grid.addWidget(self._cards[key], index // CARD_COLUMNS, index % CARD_COLUMNS)
        self._cards_box = QWidget()
        self._cards_box.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        self._chart = BaseFeeChart()
        layout = QVBoxLayout(self)
        layout.addWidget(self._waiting)
        layout.addWidget(self._cards_box)
        layout.addWidget(self._chart, 1)

    def render_stats(self, stats: Stats, status: ConnectionStatus) -> None:
        """update the cards; cheap, runs on every changed tick."""
        waiting = stats.blocks == 0
        self._waiting.setVisible(waiting)
        self._cards_box.setVisible(not waiting)
        if waiting:
            return
        cards = self._cards
        has_fee = stats.base_fee is not None
        for key in FEE_CARDS:
            cards[key].setVisible(has_fee)
        if has_fee:
            cards["base_fee"].set_html(f"<b>{format_gwei(stats.base_fee)}</b> Gwei")
            cards["trend"].set_html(_trend_html(stats))
            fees = (stats.min_base_fee, stats.avg_base_fee, stats.max_base_fee)
            cards["min_avg_max"].set_html(" / ".join(format_gwei(fee) for fee in fees))
        else:
            cards["base_fee"].set_html(_dim("n/a (no EIP-1559 base fee)"))
        cards["gas_used"].set_html(
            f"{format_percent(stats.utilization)} · avg {format_percent(stats.avg_utilization)}"
        )
        # timestamps have one-second resolution, so sub-second blocks often share one
        last = "&lt;1s" if stats.last_block_time == 0 else format_seconds(stats.last_block_time)
        cards["block_time"].set_html(f"{format_seconds(stats.block_time)} avg · last {last}")
        cards["throughput"].set_html(
            f"{format_count(stats.tps)} tx/s · {format_count(stats.tx_per_block)} tx/block"
        )
        cards["events"].set_html(_events_html(stats, status))
        cards["window"].set_html(f"{stats.blocks}/{self.metrics.window} blocks")

    def render_chart(self) -> None:
        """redraw the base fee chart; costlier, so the caller rate-limits it."""
        self._chart.render(self.metrics.blocks())


def _trend_html(stats: Stats) -> str:
    trend = trend_summary(stats)
    if trend is None:
        return _dim("—")
    text, level = trend
    if level is None:
        return _dim(text)
    return f'<span style="color:{level_color(level)}">{escape(text)}</span>'


def _events_html(stats: Stats, status: ConnectionStatus) -> str:
    if status.logs is LogsState.OFF:
        return _dim("off")
    if status.logs is LogsState.UNAVAILABLE:
        return f'<span style="color:{level_color("yellow")}">unavailable on this node</span>'
    return f"{format_rate(stats.event_rate)} · {format_count(stats.events_per_block)}/block"


def _dim(text: str) -> str:
    return f'<span style="color:{dim_color()}">{escape(text)}</span>'
