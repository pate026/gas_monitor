"""tests for the GUI base fee chart; skipped when PySide6 or pyqtgraph is not installed."""

from __future__ import annotations

import os
import unittest

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
    from PySide6.QtWidgets import QApplication

    from gui.gas_panel import BaseFeeChart
except ImportError as exc:  # missing package or Qt system library
    raise unittest.SkipTest(f"GUI dependencies missing: {exc.name or exc}") from None

from ws_client import Block

GWEI = 1_000_000_000


def _block(number: int) -> Block:
    return Block(
        number=number,
        hash=f"0x{number:x}",
        timestamp=number * 12,
        base_fee=GWEI,
        gas_used=15_000_000,
        gas_limit=30_000_000,
        received_at=0.0,
    )


class BaseFeeChartTest(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])  # type: ignore[assignment]

    def setUp(self) -> None:
        self.chart = BaseFeeChart()

    def _x_range(self) -> tuple[float, float]:
        low, high = self.chart.viewRange()[0]
        return low, high

    def test_single_block_keeps_the_latest_block_at_the_edge(self) -> None:
        self.chart.render([_block(100)])
        low, high = self._x_range()
        # zero-width ranges used to be centered on the point: -0.5 to 0.5 blocks ago
        self.assertGreater(low, -0.1)
        self.assertGreaterEqual(high, 1.0)

    def test_window_sets_the_x_range(self) -> None:
        self.chart.render([_block(number) for number in range(110, 100, -1)])  # newest first
        low, high = self._x_range()
        self.assertGreater(low, -0.5)
        self.assertAlmostEqual(high, 9 * 1.02, delta=0.3)

    def test_no_blocks_clears_the_curve(self) -> None:
        self.chart.render([_block(100)])
        self.chart.render([])
        xs = self.chart.getPlotItem().listDataItems()[0].getData()[0]
        self.assertTrue(xs is None or len(xs) == 0)  # pyqtgraph returns None for no data


if __name__ == "__main__":
    unittest.main()
