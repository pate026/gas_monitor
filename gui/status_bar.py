"""status bar: connection state, chain, endpoint, latest block and its age."""

from __future__ import annotations

import math
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy

from formatting import Level, age_level, chain_label, format_seconds
from gui.theme import PAUSED_COLOR, PAUSED_TEXT_COLOR, dim_color, level_color
from metrics import Metrics, Stats
from ws_client import ConnectionState, ConnectionStatus

SEPARATOR = "&nbsp;&nbsp;│&nbsp;&nbsp;"


class StatusBar(QFrame):
    """one-line status strip at the top of the window."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("status")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._left = QLabel()
        self._right = QLabel()
        for label in (self._left, self._right):
            label.setTextFormat(Qt.TextFormat.RichText)
        # long errors or endpoints are clipped instead of widening the window
        self._left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self._left, 1)
        layout.addWidget(self._right)

    def render(
        self, status: ConnectionStatus, metrics: Metrics, stats: Stats, now: float, *, paused: bool
    ) -> None:
        _set_html(self._left, _left_html(status, now))
        _set_html(self._right, _right_html(metrics, stats, now, paused=paused))


def _left_html(status: ConnectionStatus, now: float) -> str:
    dim = dim_color()
    if status.state is ConnectionState.CONNECTED:
        parts = [_span("● connected", level_color("green"), bold=True)]
    elif status.state is ConnectionState.RECONNECTING:
        wait = max(0, math.ceil((status.retry_at or now) - now))
        parts = [_span(f"● reconnecting in {wait}s", level_color("red"), bold=True)]
    elif status.state is ConnectionState.CONNECTING:
        parts = [_span("◌ connecting…", level_color("yellow"), bold=True)]
    else:
        parts = [_span("○ stopped", dim)]
    if status.state is not ConnectionState.CONNECTED:
        if status.attempt > 1:
            parts.append(_span(f" (attempt {status.attempt})", dim))
        if status.last_error:
            parts.append(_span(f" · {status.last_error}", level_color("red")))
    parts.append(f'<span style="color:{dim}">{SEPARATOR}</span>')
    parts.append(escape(chain_label(status.chain_id)))
    parts.append(f'<span style="color:{dim}">{SEPARATOR}</span>')
    parts.append(_span(status.endpoint, dim))
    return "".join(parts)


def _right_html(metrics: Metrics, stats: Stats, now: float, *, paused: bool) -> str:
    parts: list[str] = []
    latest = metrics.latest
    if latest is not None:
        parts.append(f"<b>block #{latest.number}</b>")
    if metrics.last_block_at is not None:
        age = now - metrics.last_block_at
        level: Level = age_level(age, stats.block_time)
        text = f"{format_seconds(age)} ago"
        parts.append("&nbsp;&nbsp;" + _span(text, level_color(level), bold=level == "red"))
    if paused:
        style = f"background:{PAUSED_COLOR}; color:{PAUSED_TEXT_COLOR};"
        badge = f'<span style="{style}"><b>&nbsp;paused&nbsp;</b></span>'
        parts.append("&nbsp;&nbsp;" + badge)
    return "".join(parts)


def _span(text: str, color: str, *, bold: bool = False) -> str:
    weight = "font-weight:bold;" if bold else ""
    return f'<span style="color:{color};{weight}">{escape(text)}</span>'


def _set_html(label: QLabel, html: str) -> None:
    # setText always repaints, so skip it when the text did not change
    if label.text() != html:
        label.setText(html)
