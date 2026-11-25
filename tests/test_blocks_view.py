"""tests for the GUI blocks panel scroll position; skipped when PySide6 is not installed."""

from __future__ import annotations

import os
import unittest

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
    from PySide6.QtWidgets import QApplication, QTableView

    from gui.blocks_view import BlocksPanel
except ImportError as exc:  # missing package or Qt system library
    raise unittest.SkipTest(f"GUI dependencies missing: {exc.name or exc}") from None

from metrics import Metrics
from ws_client import Block

PANEL_SIZE = (600, 300)  # small enough that the table scrolls


def _block(number: int) -> Block:
    return Block(
        number=number,
        hash=f"0x{number:x}",
        timestamp=1_700_000_000 + number * 12,
        base_fee=1_000_000_000,
        gas_used=15_000_000,
        gas_limit=30_000_000,
        received_at=0.0,
    )


class BlocksPanelScrollTest(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])  # type: ignore[assignment]

    def setUp(self) -> None:
        self.metrics = Metrics(window=100)
        self.panel = BlocksPanel(self.metrics)
        self.panel.resize(*PANEL_SIZE)
        self.panel.show()
        self.table = self.panel.findChild(QTableView)
        self.bar = self.table.verticalScrollBar()

    def tearDown(self) -> None:
        self.panel.close()

    def _render(self) -> None:
        self.panel.render()
        self.app.processEvents()  # let the scroll bar range follow the new row count

    def _top_block(self) -> int:
        return int(self.table.model().data(self.table.model().index(self.table.rowAt(0), 0)))

    def _scroll_to(self, row: int) -> None:
        self.bar.setValue(row)
        self.app.processEvents()

    def test_new_heads_keep_the_rows_being_read(self) -> None:
        for number in range(100, 140):
            self.metrics.on_block(_block(number), live=True)
        self._render()
        self._scroll_to(10)
        top = self._top_block()
        for number in range(140, 143):
            self.metrics.on_block(_block(number), live=True)
        self._render()
        self.assertEqual(self._top_block(), top)

    def test_filled_gap_keeps_the_rows_being_read(self) -> None:
        gap = range(130, 135)  # above the rows being read, so filling it pushes them down
        for number in range(100, 140):
            if number not in gap:
                self.metrics.on_block(_block(number), live=True)
        self._render()
        self._scroll_to(10)
        top = self._top_block()
        for number in gap:
            self.metrics.on_block(_block(number), live=False)  # backfill: the model resets
        self._render()
        self.assertEqual(self.table.model().rowCount(), 40)
        self.assertEqual(self._top_block(), top)

    def test_at_the_top_the_newest_block_stays_in_view(self) -> None:
        for number in range(100, 140):
            self.metrics.on_block(_block(number), live=True)
        self._render()
        self.metrics.on_block(_block(140), live=True)
        self._render()
        self.assertEqual(self._top_block(), 140)


if __name__ == "__main__":
    unittest.main()
