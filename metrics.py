"""rolling window of recent blocks and the gas / network metrics derived from it."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice

from ws_client import Block, LogEvent

WEI_PER_GWEI = 1_000_000_000
TREND_BLOCKS = 10  # most recent blocks fitted to classify the base fee trend
TREND_THRESHOLD = 0.005  # fitted relative change per block that counts as rising / falling
EVENT_RATE_WINDOW = 60.0  # seconds of history behind the event rate
LOG_BUFFER = 500  # recent logs kept for the UI feed
LOG_STREAM_ACTIVE = 3.0  # seconds since the last log for the stream to count as busy


@dataclass(frozen=True, slots=True)
class Stats:
    """summary of the rolling window at one point in time, ready for display."""

    blocks: int = 0
    base_fee: float | None = None  # Gwei, latest block
    min_base_fee: float | None = None
    avg_base_fee: float | None = None
    max_base_fee: float | None = None
    trend: str = "unknown"  # rising | falling | stable | unknown
    trend_change: float | None = None  # fitted fractional change across the trend span
    utilization: float | None = None  # latest block, gasUsed / gasLimit
    avg_utilization: float | None = None
    block_time: float | None = None  # average seconds per block
    last_block_time: float | None = None
    tx_per_block: float | None = None
    tps: float | None = None
    event_rate: float | None = None  # events per second
    events_per_block: float | None = None
    base_fee_history: tuple[float, ...] = ()  # Gwei, oldest first


class Metrics:
    """in-memory chain state fed by the WebSocket client (an EventSink) and read by the TUI."""

    def __init__(self, window: int = 50, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.window = window
        self._clock = clock
        self._blocks: dict[int, Block] = {}
        self._event_counts: OrderedDict[str, int] = OrderedDict()  # block hash -> logs seen
        self._recent_logs: deque[LogEvent] = deque(maxlen=LOG_BUFFER)
        self._rate_buckets: deque[list[int]] = deque()  # [whole clock second, events] pairs
        self._rate_origin: float | None = None
        self.logs_active = False
        self.highest_log_block: int | None = None  # logs arrive in block order
        self.last_log_at: float | None = None
        self.total_events = 0
        self.log_seq = 0  # bumped for every log; the feed uses it to find unseen entries
        self.blocks_version = 0  # bumped when blocks or their tx counts change
        self.version = 0  # bumped on any change; the UI uses it to skip redundant redraws
        self.last_block_at: float | None = None  # local monotonic time of the latest live block

    # --- EventSink -------------------------------------------------------------

    def on_block(self, block: Block, live: bool) -> None:
        existing = self._blocks.get(block.number)
        if existing is not None and existing.hash == block.hash:
            # seen twice (live head and backfill): merge what each source knows
            if existing.tx_count is None:
                existing.tx_count = block.tx_count
            existing.events_tracked = existing.events_tracked or block.events_tracked
        elif not live and (
            existing is not None
            or (len(self._blocks) >= self.window and block.number < min(self._blocks))
        ):
            return  # never let backfill replace a live block or refill pruned history
        else:
            if live and self._blocks and block.number <= max(self._blocks):
                # a live head at or below the tip means a chain reorganization
                for number in [n for n in self._blocks if n >= block.number]:
                    del self._blocks[number]
            self._blocks[block.number] = block
            if len(self._blocks) > self.window:
                for number in sorted(self._blocks)[: len(self._blocks) - self.window]:
                    del self._blocks[number]
        if live:
            self.last_block_at = block.received_at
        self.blocks_version += 1
        self.version += 1

    def on_tx_count(self, block_hash: str, count: int) -> None:
        for block in self._blocks.values():
            if block.hash == block_hash:
                block.tx_count = count
                self.blocks_version += 1
                self.version += 1
                return

    def on_log(self, log: LogEvent) -> None:
        if log.block_hash is not None:
            count = self._event_counts.get(log.block_hash, 0) + (-1 if log.removed else 1)
            self._event_counts[log.block_hash] = max(count, 0)
            self._event_counts.move_to_end(log.block_hash)
            while len(self._event_counts) > self.window * 4 + 64:
                self._event_counts.popitem(last=False)
        if not log.removed:
            self.total_events += 1
            self._count_event()
            self.last_log_at = self._clock()
            if log.block_number is not None and (
                self.highest_log_block is None or log.block_number > self.highest_log_block
            ):
                self.highest_log_block = log.block_number
        self._recent_logs.append(log)
        self.log_seq += 1
        self.version += 1

    def on_logs_subscription(self, active: bool) -> None:
        # restart the rate on every (re)subscription so a disconnect gap does not drag it down
        self.logs_active = active
        self._rate_buckets.clear()
        self._rate_origin = self._clock() if active else None
        self.version += 1

    # --- queries ---------------------------------------------------------------

    @property
    def latest(self) -> Block | None:
        return self._blocks[max(self._blocks)] if self._blocks else None

    def blocks(self) -> list[Block]:
        """blocks in the window, newest first."""
        return [self._blocks[number] for number in sorted(self._blocks, reverse=True)]

    def event_count(self, block: Block) -> int | None:
        """logs seen for a block, or None when the logs feed did not cover it."""
        if not block.events_tracked:
            return None
        return self._event_counts.get(block.hash, 0)

    def events_pending(self, block: Block) -> bool:
        """whether a busy log stream has not reached this block yet.

        some providers deliver logs seconds after the block header (seen on fast L2s);
        logs come in block order, so blocks above the newest logged block are incomplete.
        sparse streams (e.g. a single contract) never count as busy, so zero stays zero.
        """
        return (
            block.events_tracked
            and self.highest_log_block is not None
            and block.number > self.highest_log_block
            and self.log_stream_busy()
        )

    def log_stream_busy(self) -> bool:
        """whether logs arrived within the last few seconds."""
        return self.last_log_at is not None and self._clock() - self.last_log_at < LOG_STREAM_ACTIVE

    def recent_logs(self, limit: int) -> list[LogEvent]:
        """up to `limit` of the newest logs, oldest first."""
        count = min(max(limit, 0), len(self._recent_logs))
        return list(islice(self._recent_logs, len(self._recent_logs) - count, None))

    def event_rate(self) -> float | None:
        """events per second over the last minute (or since the subscription started)."""
        if self._rate_origin is None:
            return None
        now = self._clock()
        self._trim_rate(now)
        elapsed = min(EVENT_RATE_WINDOW, now - self._rate_origin)
        return sum(count for _, count in self._rate_buckets) / max(elapsed, 1.0)

    def stats(self) -> Stats:
        ordered = [self._blocks[number] for number in sorted(self._blocks)]  # oldest first
        if not ordered:
            return Stats(event_rate=self.event_rate())
        oldest, latest = ordered[0], ordered[-1]

        fee_points = [(b.number, b.base_fee / WEI_PER_GWEI) for b in ordered if b.base_fee is not None]
        fees = [fee for _, fee in fee_points]
        trend, trend_change = _trend(fee_points[-TREND_BLOCKS:])

        # timestamps divided by block-number span stay correct across gaps from reconnects
        span = latest.number - oldest.number
        block_time = (latest.timestamp - oldest.timestamp) / span if span > 0 else None
        previous = self._blocks.get(latest.number - 1)
        last_block_time = float(latest.timestamp - previous.timestamp) if previous else None

        tx_counts = [b.tx_count for b in ordered if b.tx_count is not None]
        tx_per_block = sum(tx_counts) / len(tx_counts) if tx_counts else None
        tps = tx_per_block / block_time if tx_per_block is not None and block_time else None

        tracked = [
            self._event_counts.get(b.hash, 0)
            for b in ordered
            if b.events_tracked and not self.events_pending(b)
        ]

        return Stats(
            blocks=len(ordered),
            base_fee=latest.base_fee / WEI_PER_GWEI if latest.base_fee is not None else None,
            min_base_fee=min(fees) if fees else None,
            avg_base_fee=sum(fees) / len(fees) if fees else None,
            max_base_fee=max(fees) if fees else None,
            trend=trend,
            trend_change=trend_change,
            utilization=latest.utilization,
            avg_utilization=sum(b.utilization for b in ordered) / len(ordered),
            block_time=block_time,
            last_block_time=last_block_time,
            tx_per_block=tx_per_block,
            tps=tps,
            event_rate=self.event_rate(),
            events_per_block=sum(tracked) / len(tracked) if tracked else None,
            base_fee_history=tuple(fees),
        )

    # --- internals -------------------------------------------------------------

    def _count_event(self) -> None:
        now = self._clock()
        second = int(now)
        if self._rate_buckets and self._rate_buckets[-1][0] == second:
            self._rate_buckets[-1][1] += 1
        else:
            self._rate_buckets.append([second, 1])
            self._trim_rate(now)

    def _trim_rate(self, now: float) -> None:
        cutoff = now - EVENT_RATE_WINDOW
        while self._rate_buckets and self._rate_buckets[0][0] < cutoff:
            self._rate_buckets.popleft()


def _trend(points: list[tuple[int, float]]) -> tuple[str, float | None]:
    """classify the base fee trend from a least-squares fit of Gwei against block number."""
    if len(points) < 3:
        return "unknown", None
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx == 0 or mean_y <= 0:
        return "stable", 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    per_block = slope / mean_y
    change = per_block * (points[-1][0] - points[0][0])
    if per_block > TREND_THRESHOLD:
        return "rising", change
    if per_block < -TREND_THRESHOLD:
        return "falling", change
    return "stable", change
