"""tests for the GUI blocks table model; skipped when PySide6 is not installed."""

from __future__ import annotations

import os
import unittest

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QAbstractItemModelTester
    from PySide6.QtWidgets import QApplication

    from gui.blocks_model import COLUMN_KEYS, NOT_COVERED, PENDING, BlocksModel, row_diff
    from gui.theme import apply_dark_theme, level_color
except ModuleNotFoundError as exc:  # the TUI alone does not need PySide6
    raise unittest.SkipTest(f"GUI dependencies missing: {exc.name}") from None

from metrics import Metrics
from ws_client import Block, LogEvent

GWEI = 1_000_000_000
TIMESTAMP_BASE = 1_700_000_000


def _block(number: int, *, fee: int | None = GWEI, tx_count: int | None = None, tag: str = "") -> Block:
    return Block(
        number=number,
        hash=f"0x{number:x}{tag}",
        timestamp=TIMESTAMP_BASE + number * 12,
        base_fee=fee,
        gas_used=15_000_000,
        gas_limit=30_000_000,
        received_at=0.0,
        tx_count=tx_count,
    )


class RowDiffTest(unittest.TestCase):
    def test_simple_changes(self) -> None:
        self.assertEqual(row_diff([], []), (0, 0, 0))
        self.assertEqual(row_diff([], [3, 2, 1]), (0, 3, 0))
        self.assertEqual(row_diff([3, 2, 1], [5, 4, 3, 2]), (1, 2, 0))  # new heads, oldest pruned
        self.assertEqual(row_diff([5, 4], [5, 4, 3, 2]), (0, 0, 2))  # backfill below

    def test_changes_that_need_a_reset(self) -> None:
        self.assertIsNone(row_diff([5, 4, 2], [5, 4, 3, 2]))  # gap filled in the middle
        self.assertIsNone(row_diff([5, 4, 3], [4, 3]))  # top removed by a reorg
        self.assertIsNone(row_diff([5, 4], [9, 8]))  # nothing in common


class BlocksModelTest(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])  # type: ignore[assignment]
        apply_dark_theme(cls.app)

    def setUp(self) -> None:
        self.metrics = Metrics(window=5)
        self.model = BlocksModel(self.metrics)
        # fails the test on any inconsistent model signal (rows, indexes, data roles)
        self.tester = QAbstractItemModelTester(
            self.model, QAbstractItemModelTester.FailureReportingMode.Fatal
        )

    def _numbers(self) -> list[int]:
        return [int(self.model.data(self.model.index(row, 0))) for row in range(self.model.rowCount())]

    def _cell(self, row: int, key: str, role: Qt.ItemDataRole = Qt.ItemDataRole.DisplayRole) -> object:
        return self.model.data(self.model.index(row, COLUMN_KEYS.index(key)), role)

    def test_new_heads_are_inserted_at_the_top(self) -> None:
        for number in (100, 101):
            self.metrics.on_block(_block(number), live=True)
        self.assertEqual(self.model.refresh(), 2)
        self.metrics.on_block(_block(102), live=True)
        self.assertEqual(self.model.refresh(), 1)
        self.assertEqual(self._numbers(), [102, 101, 100])

    def test_backfill_appends_and_window_prunes(self) -> None:
        self.metrics.on_block(_block(100), live=True)
        self.model.refresh()
        for number in (99, 98):
            self.metrics.on_block(_block(number), live=False)
        self.assertEqual(self.model.refresh(), 0)  # nothing new at the top: no scroll shift
        for number in range(101, 104):
            self.metrics.on_block(_block(number), live=True)
        self.assertEqual(self.model.refresh(), 3)
        self.assertEqual(self._numbers(), [103, 102, 101, 100, 99])

    def test_reorg_resets_without_reporting_new_rows(self) -> None:
        for number in range(100, 104):
            self.metrics.on_block(_block(number), live=True)
        self.model.refresh()
        self.metrics.on_block(_block(102, tag="b"), live=True)  # replaces 102, drops 103
        self.assertEqual(self.model.refresh(), 0)
        self.assertEqual(self._numbers(), [102, 101, 100])

    def test_cells_follow_the_tui(self) -> None:
        self.metrics.on_block(_block(100), live=True)
        self.metrics.on_block(_block(101, fee=int(1.1 * GWEI)), live=True)
        self.model.refresh()
        self.assertEqual(self._cell(0, "base_fee"), "1.100")
        self.assertEqual(self._cell(0, "delta"), "+10.0%")
        delta_color = self._cell(0, "delta", Qt.ItemDataRole.ForegroundRole)
        self.assertEqual(delta_color.name(), level_color("red"))  # type: ignore[attr-defined]
        self.assertEqual(self._cell(1, "delta"), "")  # no parent block in the window
        self.assertEqual(self._cell(0, "gas_used"), "50.0%")
        self.assertEqual(self._cell(0, "txs"), PENDING)  # tx count not fetched yet
        self.assertEqual(self._cell(0, "events"), NOT_COVERED)  # logs did not cover it

    def test_late_tx_counts_and_events_show_after_refresh(self) -> None:
        self.metrics.on_logs_subscription(True)
        block = _block(100)
        block.events_tracked = True
        self.metrics.on_block(block, live=True)
        self.model.refresh()
        self.assertEqual(self._cell(0, "events"), "0")
        self.metrics.on_tx_count(block.hash, 1234)
        log = LogEvent(100, block.hash, "0xt", 0, "0x" + "11" * 20, (), "0x", False)
        for _ in range(3):
            self.metrics.on_log(log)
        self.model.refresh()
        self.assertEqual(self._cell(0, "txs"), "1,234")
        self.assertEqual(self._cell(0, "events"), "3")


if __name__ == "__main__":
    unittest.main()
