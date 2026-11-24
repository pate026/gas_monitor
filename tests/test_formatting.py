"""tests for the ui-independent formatting helpers shared by the TUI and the GUI."""

from __future__ import annotations

import unittest

from config import Config
from formatting import (
    age_level,
    chain_label,
    clock_time,
    fee_change,
    feed_line,
    feed_scope,
    format_count,
    format_gwei,
    format_percent,
    format_rate,
    format_seconds,
    logs_notice,
    more_events_text,
    trend_summary,
    utilization_level,
)
from metrics import Stats
from ws_client import ConnectionStatus, LogEvent, LogsState

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUM = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _transfer_log(*, block: int | None = 123, removed: bool = False) -> LogEvent:
    padded = "0x" + "00" * 12 + USDC[2:]
    return LogEvent(
        block_number=block,
        block_hash="0xh",
        tx_hash="0xt",
        log_index=0,
        address=USDC,
        topics=(TRANSFER_TOPIC, padded, padded),
        data="0x" + "00" * 31 + "05",
        removed=removed,
    )


class NumberFormattingTest(unittest.TestCase):
    def test_format_gwei_keeps_about_four_significant_digits(self) -> None:
        cases = {
            None: "—",
            0: "0",
            -1: "0",
            1e-9: "0.000000001",
            0.00123: "0.00123",
            0.125: "0.125",
            1.5: "1.500",
            12.345: "12.35",
            150: "150.0",
            1234: "1,234",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_gwei(value), expected)

    def test_format_seconds(self) -> None:
        cases = {
            None: "—",
            0: "0s",
            12: "12s",
            0.49: "0.49s",
            2.5: "2.5s",
            45.6: "46s",
            125: "2m 05s",
            3725: "1h 02m",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_seconds(value), expected)

    def test_format_rate_switches_to_per_minute_below_one_per_second(self) -> None:
        self.assertEqual(format_rate(None), "—")
        self.assertEqual(format_rate(0.5), "30.0/min")
        self.assertEqual(format_rate(2.5), "2.5/s")
        self.assertEqual(format_rate(1500), "1,500/s")

    def test_format_count_and_percent(self) -> None:
        self.assertEqual(format_count(None), "—")
        self.assertEqual(format_count(9.94), "9.9")
        self.assertEqual(format_count(1234.4), "1,234")
        self.assertEqual(format_percent(None), "—")
        self.assertEqual(format_percent(0.4972), "49.7%")

    def test_chain_label(self) -> None:
        self.assertEqual(chain_label(None), "chain ?")
        self.assertEqual(chain_label(1), "Ethereum · chain 1")
        self.assertEqual(chain_label(999_999), "chain 999999")

    def test_clock_time_survives_invalid_timestamps(self) -> None:
        self.assertEqual(clock_time(10**20), "?")
        self.assertRegex(clock_time(1_700_000_000), r"^\d\d:\d\d:\d\d$")


class LevelTest(unittest.TestCase):
    def test_utilization_level_around_the_eip1559_target(self) -> None:
        self.assertEqual(utilization_level(0.5), "green")
        self.assertEqual(utilization_level(0.51), "yellow")
        self.assertEqual(utilization_level(0.9), "red")

    def test_age_level_relative_to_block_time(self) -> None:
        self.assertEqual(age_level(23, 12), "green")
        self.assertEqual(age_level(24, 12), "yellow")
        self.assertEqual(age_level(60, 12), "red")
        self.assertEqual(age_level(23, None), "green")  # assumes 12s blocks
        self.assertEqual(age_level(2, 0.2), "yellow")  # at least one second per block

    def test_fee_change(self) -> None:
        self.assertIsNone(fee_change(None, 100))
        self.assertIsNone(fee_change(100, None))
        self.assertIsNone(fee_change(100, 0))
        self.assertAlmostEqual(fee_change(110, 100) or 0.0, 0.1)

    def test_trend_summary(self) -> None:
        self.assertIsNone(trend_summary(Stats()))
        self.assertEqual(
            trend_summary(Stats(blocks=50, trend="rising", trend_change=0.031)),
            ("▲ rising +3.1% (10 blocks)", "red"),
        )
        self.assertEqual(
            trend_summary(Stats(blocks=4, trend="falling", trend_change=-0.2)),
            ("▼ falling -20.0% (4 blocks)", "green"),
        )
        self.assertEqual(
            trend_summary(Stats(blocks=50, trend="stable", trend_change=0.0)),
            ("◆ stable +0.0% (10 blocks)", None),
        )


class FeedTest(unittest.TestCase):
    def test_feed_line_decodes_a_transfer(self) -> None:
        line = feed_line(_transfer_log(), compact=False)
        self.assertEqual(line.block, "       123")
        self.assertEqual(line.address, USDC_CHECKSUM)
        self.assertEqual(line.topic.rstrip(), "0xddf252ad…b3ef")
        self.assertEqual(line.name.rstrip(), "Transfer")
        self.assertTrue(line.known)
        self.assertEqual([label for label, _ in line.args], ["from", "to", "value"])
        self.assertEqual(line.args[-1][1], "5")
        self.assertFalse(line.removed)

    def test_feed_line_compact_pending_and_removed(self) -> None:
        line = feed_line(_transfer_log(block=None, removed=True), compact=True)
        self.assertEqual(line.block.strip(), "pending")
        self.assertEqual(line.address, "0xA0b8…eB48")
        self.assertTrue(line.removed)

    def test_feed_line_anonymous_event(self) -> None:
        log = LogEvent(5, "0xh", "0xt", 0, USDC, (), "0x", False)
        line = feed_line(log, compact=True)
        self.assertEqual(line.topic.rstrip(), "anonymous")
        self.assertEqual(line.name.rstrip(), "unknown")
        self.assertFalse(line.known)

    def test_more_events_text_lines_up_with_the_address_column(self) -> None:
        self.assertEqual(more_events_text(1234), " " * 12 + "⋯ 1,234 more events not shown")

    def test_feed_scope(self) -> None:
        url = "wss://example.org"
        self.assertEqual(feed_scope(Config(ws_url=url, subscribe_logs=False)), "logs off")
        self.assertEqual(feed_scope(Config(ws_url=url)), "all contracts, sampled when busy")
        self.assertEqual(feed_scope(Config(ws_url=url, contract_addresses=(USDC,))), USDC_CHECKSUM)
        two = Config(ws_url=url, contract_addresses=(USDC, "0x" + "11" * 20))
        self.assertEqual(feed_scope(two), "2 contracts")

    def test_logs_notice(self) -> None:
        status = ConnectionStatus(endpoint="wss://example.org", logs=LogsState.PENDING)
        self.assertIsNone(logs_notice(status, "scope"))
        status.logs = LogsState.ACTIVE
        self.assertEqual(logs_notice(status, "scope"), ("· subscribed to logs from scope", None))
        status.logs, status.logs_error = LogsState.UNAVAILABLE, "method not found"
        self.assertEqual(
            logs_notice(status, "scope"),
            ("· the node rejected the logs subscription: method not found", "yellow"),
        )
        status.logs = LogsState.OFF
        self.assertEqual(logs_notice(status, "scope"), ("· logs subscription disabled", None))


if __name__ == "__main__":
    unittest.main()
