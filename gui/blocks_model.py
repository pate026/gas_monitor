"""recent blocks table: an incrementally updated model and the gas used bar delegate."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from formatting import (
    FEE_CHANGE_FLAT,
    UTILIZATION_TARGET,
    clock_time,
    fee_change,
    format_gwei,
    utilization_level,
)
from gui.theme import BLOCK_NUMBER_COLOR, dim_color, level_color
from metrics import WEI_PER_GWEI, Metrics
from ws_client import Block

# (key, header, sample text used to size the column, right-aligned)
COLUMNS = (
    ("block", "block", "000000000", False),
    ("time", "time", "00:00:00", False),
    ("base_fee", "base fee", "0.0000000", True),
    ("delta", "Δ", "+100.0%", True),
    ("gas_used", "gas used", "", False),
    ("txs", "txs", "00,000", True),
    ("events", "events", "000,000", True),
)
COLUMN_KEYS = tuple(key for key, _, _, _ in COLUMNS)
GAS_USED_COLUMN = COLUMN_KEYS.index("gas_used")
PENDING = "…"  # data still on its way
NOT_COVERED = "–"  # the logs feed did not cover this block
FRACTION_ROLE = Qt.ItemDataRole.UserRole  # gas used as a 0..1 fraction, for the delegate

BAR_WIDTH = 90  # pixels
BAR_HEIGHT = 8
BAR_PERCENT_SAMPLE = "100.0%"
CELL_PADDING = 6

_Index = QModelIndex | QPersistentModelIndex


class BlocksModel(QAbstractTableModel):
    """blocks in the metrics window, newest first; refresh() syncs it without a full reset."""

    def __init__(self, metrics: Metrics) -> None:
        super().__init__()
        self._metrics = metrics
        self._rows: list[Block] = []
        self._by_number: dict[int, Block] = {}
        self._dim = QColor(dim_color())
        self._number_color = QColor(BLOCK_NUMBER_COLOR)
        self._fee_colors = {True: QColor(level_color("red")), False: QColor(level_color("green"))}
        self._bold = QFont()
        self._bold.setBold(True)

    # --- Qt model interface ------------------------------------------------------

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: N802 (Qt override)
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: N802 (Qt override)
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802 (Qt override)
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation is not Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _alignment(COLUMNS[section][3])
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        block = self._rows[index.row()]
        key = COLUMN_KEYS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(block, key)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._color(block, key)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _alignment(COLUMNS[index.column()][3])
        if role == Qt.ItemDataRole.FontRole and key == "block":
            return self._bold
        if role == FRACTION_ROLE and key == "gas_used":
            return block.utilization
        return None

    # --- updates -------------------------------------------------------------------

    def refresh(self) -> int:
        """sync the rows with the metrics window; returns how many rows were added at the top."""
        new = self._metrics.blocks()
        old_numbers = [block.number for block in self._rows]
        new_numbers = [block.number for block in new]
        diff = row_diff(old_numbers, new_numbers)
        if diff is None:
            self.beginResetModel()
            self._set_rows(new)
            self.endResetModel()
            return 0
        removed_tail, added_top, added_bottom = diff
        kept = len(old_numbers) - removed_tail
        if removed_tail:
            self.beginRemoveRows(QModelIndex(), kept, len(old_numbers) - 1)
            self._set_rows(self._rows[:kept])
            self.endRemoveRows()
        if added_top:
            self.beginInsertRows(QModelIndex(), 0, added_top - 1)
            self._set_rows(new[:added_top] + self._rows)
            self.endInsertRows()
        if added_bottom:
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + added_bottom - 1)
            self._set_rows(self._rows + new[len(new) - added_bottom :])
            self.endInsertRows()
        # same block numbers, but the objects may be new (reorgs) and late data may have arrived
        self._set_rows(new)
        if new:
            self.dataChanged.emit(self.index(0, 0), self.index(len(new) - 1, len(COLUMNS) - 1))
        return added_top

    def number_at(self, row: int) -> int | None:
        """block number shown in a row, or None outside the table."""
        return self._rows[row].number if 0 <= row < len(self._rows) else None

    def row_of(self, number: int) -> int | None:
        """row that shows a block number, or None when it is not in the table."""
        return next((row for row, block in enumerate(self._rows) if block.number == number), None)

    def _set_rows(self, rows: list[Block]) -> None:
        self._rows = rows
        self._by_number = {block.number: block for block in rows}

    # --- cell contents -----------------------------------------------------------

    def _text(self, block: Block, key: str) -> str:
        if key == "block":
            return str(block.number)
        if key == "time":
            return clock_time(block.timestamp)
        if key == "base_fee":
            return format_gwei(block.base_fee / WEI_PER_GWEI) if block.base_fee is not None else "—"
        if key == "delta":
            change = self._fee_change(block)
            if change is None:
                return ""
            return "0.0%" if abs(change) < FEE_CHANGE_FLAT else f"{change * 100:+.1f}%"
        if key == "gas_used":
            return f"{block.utilization * 100:.1f}%"
        if key == "txs":
            return PENDING if block.tx_count is None else f"{block.tx_count:,}"
        events = self._metrics.event_count(block)
        if events is None:
            return NOT_COVERED
        return PENDING if self._metrics.events_pending(block) else f"{events:,}"

    def _color(self, block: Block, key: str) -> QColor | None:
        if key == "block":
            return self._number_color
        if key == "time":
            return self._dim
        if key == "delta":
            change = self._fee_change(block)
            if change is None or abs(change) < FEE_CHANGE_FLAT:
                return self._dim
            return self._fee_colors[change > 0]
        if key in ("txs", "events") and self._text(block, key) in (PENDING, NOT_COVERED):
            return self._dim
        return None

    def _fee_change(self, block: Block) -> float | None:
        previous = self._by_number.get(block.number - 1)
        return None if previous is None else fee_change(block.base_fee, previous.base_fee)


class GasUsedDelegate(QStyledItemDelegate):
    """gas used as a bar with a mark at the EIP-1559 target, plus the percentage."""

    def __init__(self) -> None:
        super().__init__()
        self._track = QColor(dim_color())
        self._track.setAlpha(70)
        self._target = QColor(dim_color())

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        # let the style draw the background (alternating rows), but not the text
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = option.widget  # type: ignore[attr-defined]
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        fraction = index.data(FRACTION_ROLE)
        if fraction is None:
            return
        cell: QRect = option.rect  # type: ignore[attr-defined]
        rect = cell.adjusted(CELL_PADDING, 0, -CELL_PADDING, 0)
        bar = QRect(rect.left(), rect.center().y() - BAR_HEIGHT // 2 + 1, BAR_WIDTH, BAR_HEIGHT)
        filled = QRect(bar)
        filled.setWidth(round(bar.width() * min(max(fraction, 0.0), 1.0)))
        painter.save()
        painter.fillRect(bar, self._track)
        painter.fillRect(filled, QColor(level_color(utilization_level(fraction))))
        target_x = bar.left() + round(bar.width() * UTILIZATION_TARGET)
        painter.setPen(self._target)
        painter.drawLine(target_x, bar.top() - 2, target_x, bar.bottom() + 2)
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))  # type: ignore[attr-defined]
        text_left = bar.right() + CELL_PADDING
        text_rect = QRect(text_left, rect.top(), rect.right() - text_left, rect.height())
        align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(text_rect, align, f"{fraction * 100:.1f}%")
        painter.restore()

    def preferred_width(self, font_metrics: QFontMetrics) -> int:
        return BAR_WIDTH + font_metrics.horizontalAdvance(BAR_PERCENT_SAMPLE) + 3 * CELL_PADDING


def row_diff(old: list[int], new: list[int]) -> tuple[int, int, int] | None:
    """(rows removed from the bottom, added at the top, added at the bottom) turning old into new.

    None when the change is not that simple (a filled gap, a reorg that renumbered rows), in
    which case the caller resets the model.
    """
    new_set = set(new)
    kept = [number for number in old if number in new_set]
    if not kept:
        return None if old else (0, len(new), 0)
    if old[: len(kept)] != kept:
        return None  # rows disappeared from the top or the middle
    start = new.index(kept[0])
    if new[start : start + len(kept)] != kept:
        return None
    return len(old) - len(kept), start, len(new) - start - len(kept)


def _alignment(right: bool) -> Qt.AlignmentFlag:
    horizontal = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
    return horizontal | Qt.AlignmentFlag.AlignVCenter
