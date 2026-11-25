"""recent blocks panel: the table view around BlocksModel, keeping the reader's scroll position."""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QGroupBox, QHeaderView, QTableView, QVBoxLayout

from gui.blocks_model import COLUMNS, GAS_USED_COLUMN, BlocksModel, GasUsedDelegate
from metrics import Metrics

COLUMN_PADDING = 24  # pixels around the widest sample text of a column
ROW_PADDING = 8


class BlocksPanel(QGroupBox):
    """scrollable table of the blocks in the window, newest first."""

    def __init__(self, metrics: Metrics) -> None:
        super().__init__("recent blocks · base fee in Gwei")
        self._model = BlocksModel(metrics)
        self._delegate = GasUsedDelegate()
        table = QTableView()
        table.setModel(self._model)
        table.setItemDelegateForColumn(GAS_USED_COLUMN, self._delegate)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        rows = table.verticalHeader()
        rows.hide()
        # fixed sizes: Qt never measures cell contents, which keeps updates cheap
        rows.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        rows.setDefaultSectionSize(table.fontMetrics().height() + ROW_PADDING)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        font_metrics = table.fontMetrics()
        for column, (_, title, sample, _) in enumerate(COLUMNS):
            width = max(font_metrics.horizontalAdvance(sample), font_metrics.horizontalAdvance(title))
            table.setColumnWidth(column, width + COLUMN_PADDING)
        table.setColumnWidth(GAS_USED_COLUMN, self._delegate.preferred_width(table.fontMetrics()))
        self._table = table
        layout = QVBoxLayout(self)
        layout.addWidget(table)

    def render(self) -> None:
        """sync the table with the metrics window, without moving the rows being read."""
        bar = self._table.verticalScrollBar()
        position = bar.value()  # scrolling per item: the first visible row
        anchor = self._model.number_at(position) if position > 0 else None
        added_top = self._model.refresh()
        # at the top the newest block stays in view; scrolled down, keep the block that was
        # at the top of the screen there, also after a model reset (a filled gap)
        row = self._model.row_of(anchor) if anchor is not None else None
        if row is not None:
            bar.setValue(row)
        elif position > 0 and added_top:
            bar.setValue(position + added_top)
