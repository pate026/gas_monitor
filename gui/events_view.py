"""live events panel: a bounded feed of decoded logs, sampled like the TUI's."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QGroupBox, QPlainTextEdit, QVBoxLayout

from config import Config
from formatting import Level, feed_line, feed_scope, logs_notice, more_events_text
from gui.theme import BLOCK_NUMBER_COLOR, EVENT_NAME_COLOR, dim_color, level_color
from metrics import Metrics
from ws_client import ConnectionStatus, LogEvent, LogsState

FEED_MAX_LINES = 250
FEED_LINES_PER_REFRESH = 10  # newest events written per redraw, so busy chains stay readable
FEED_FULL_ADDRESS_MIN_WIDTH = 130  # in characters; narrower feeds show shortened addresses
# lines from the bottom that still count as following the feed; layout changes such as the
# horizontal scrollbar appearing move the bottom by a line or two without the user scrolling
FOLLOW_SLACK_LINES = 3

Segment = tuple[str, QTextCharFormat]


class EventsPanel(QGroupBox):
    """scrolling feed of the newest events; older lines drop off after FEED_MAX_LINES."""

    def __init__(self, config: Config, metrics: Metrics) -> None:
        self._scope = feed_scope(config)
        super().__init__(f"live events · {self._scope}")
        self.metrics = metrics
        self._seq = 0  # metrics.log_seq that the feed has caught up to
        self._logs_state_seen: LogsState | None = None
        self._feed = QPlainTextEdit()
        self._feed.setReadOnly(True)
        self._feed.setMaximumBlockCount(FEED_MAX_LINES)
        self._feed.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._feed.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._formats = _Formats()
        layout = QVBoxLayout(self)
        layout.addWidget(self._feed)

    def render(self, status: ConnectionStatus) -> None:
        """append the newest events since the last call, at most FEED_LINES_PER_REFRESH."""
        lines: list[list[Segment]] = []
        notice = self._logs_notice(status)
        if notice is not None:
            lines.append(notice)
        new = self.metrics.log_seq - self._seq
        if new > 0:
            self._seq = self.metrics.log_seq
            shown = self.metrics.recent_logs(min(new, FEED_LINES_PER_REFRESH))
            if new > len(shown):
                lines.append([(more_events_text(new - len(shown)), self._formats.dim)])
            compact = self._width_in_chars() < FEED_FULL_ADDRESS_MIN_WIDTH
            lines.extend(self._event_line(log, compact) for log in shown)
        if lines:
            self._append(lines)

    def clear(self) -> None:
        """drop the shown lines; events that arrived before this are skipped."""
        self._feed.clear()
        self._seq = self.metrics.log_seq

    def _logs_notice(self, status: ConnectionStatus) -> list[Segment] | None:
        if status.logs is self._logs_state_seen:
            return None
        self._logs_state_seen = status.logs
        notice = logs_notice(status, self._scope)
        if notice is None:
            return None
        text, level = notice
        return [(text, self._formats.notice(level))]

    def _event_line(self, log: LogEvent, compact: bool) -> list[Segment]:
        parts = feed_line(log, compact=compact)
        formats = self._formats
        segments: list[Segment] = [
            (parts.block, formats.block),
            ("  " + parts.address + "  ", formats.plain),
            (parts.topic, formats.dim),
            ("  ", formats.plain),
            (parts.name, formats.name if parts.known else formats.dim),
        ]
        for label, value in parts.args:
            segments.append((f" {label}=", formats.dim))
            segments.append((value, formats.plain))
        if parts.removed:
            segments.append(("  removed by reorg", formats.removed))
        return segments

    def _append(self, lines: list[list[Segment]]) -> None:
        bar = self._feed.verticalScrollBar()
        # only auto-scroll when the user is (nearly) at the bottom, not reading older lines
        follow = bar.value() >= bar.maximum() - FOLLOW_SLACK_LINES
        cursor = QTextCursor(self._feed.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()  # one layout pass for the whole batch
        for line in lines:
            if not self._feed.document().isEmpty():
                cursor.insertBlock()
            for text, text_format in line:
                cursor.insertText(text, text_format)
        cursor.endEditBlock()
        if follow:
            # the scroll range is updated lazily, so scroll via the text cursor instead
            self._feed.moveCursor(QTextCursor.MoveOperation.End)
            self._feed.ensureCursorVisible()

    def _width_in_chars(self) -> int:
        char_width = self._feed.fontMetrics().horizontalAdvance("0")
        return self._feed.viewport().width() // max(char_width, 1)


class _Formats:
    """text formats for the feed, matching the TUI's styles."""

    def __init__(self) -> None:
        self.plain = QTextCharFormat()
        self.dim = _format(dim_color())
        self.block = _format(BLOCK_NUMBER_COLOR)
        self.name = _format(EVENT_NAME_COLOR, bold=True)
        self.removed = _format(level_color("red"), bold=True)
        self._dim_italic = _format(dim_color(), italic=True)
        self._levels = {level: _format(level_color(level)) for level in ("green", "yellow", "red")}

    def notice(self, level: Level | None) -> QTextCharFormat:
        return self._dim_italic if level is None else self._levels[level]


def _format(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(color))
    if bold:
        text_format.setFontWeight(QFont.Weight.Bold)
    text_format.setFontItalic(italic)
    return text_format
