"""Textual terminal UI: status bar, gas panel, recent blocks table and live event feed."""

from __future__ import annotations

import math
import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, RichLog, Sparkline, Static

from config import Config
from decoder import decode_log, short_address, short_hex, to_checksum_address
from metrics import TREND_BLOCKS, WEI_PER_GWEI, Metrics, Stats
from ws_client import Block, ConnectionState, EthWsClient, LogEvent, LogsState

REFRESH_INTERVAL = 0.25  # status bar tick; ingestion itself is never throttled
# on busy chains data changes on every tick, so the costlier widgets are rate-limited;
# when data is quiet the next change still shows on the following tick
TABLE_MIN_INTERVAL = 1.0  # the blocks table re-renders every cell on each rebuild
TABLE_EVENTS_INTERVAL = 2.5  # when only event counts changed (logs stream in for seconds)
FEED_MIN_INTERVAL = 0.5
GAS_MIN_INTERVAL = 0.5
GAS_IDLE_INTERVAL = 1.0  # redraw the gas panel at least this often (the event rate decays)
SPARKLINE_MIN_INTERVAL = 1.0  # the sparkline is computed cell by cell in Python
FEED_LINES_PER_REFRESH = 10  # newest events written per feed redraw, so busy chains stay readable
# every write that grows the log's line count costs a full-screen layout pass in Textual,
# so keep the scrollback short: once it is full, writes only replace lines and stay cheap
FEED_MAX_LINES = 250
FEED_FULL_ADDRESS_MIN_WIDTH = 130  # narrower feeds show shortened contract addresses
WIDE_LAYOUT_MIN_WIDTH = 126  # below this the gas panel stacks above the blocks table
BAR_WIDTH = 10
SPARKLINE_FLAT_RANGE = 0.005  # relative min-max range below which the base fee chart is drawn flat

CHAIN_NAMES = {
    1: "Ethereum",
    10: "OP Mainnet",
    56: "BNB Smart Chain",
    100: "Gnosis",
    130: "Unichain",
    137: "Polygon",
    146: "Sonic",
    324: "zkSync Era",
    480: "World Chain",
    1101: "Polygon zkEVM",
    5000: "Mantle",
    8453: "Base",
    34443: "Mode",
    42161: "Arbitrum One",
    42170: "Arbitrum Nova",
    43114: "Avalanche C-Chain",
    59144: "Linea",
    81457: "Blast",
    534352: "Scroll",
    7777777: "Zora",
    # testnets and local development chains
    17000: "Holesky",
    560048: "Hoodi",
    11155111: "Sepolia",
    84532: "Base Sepolia",
    421614: "Arbitrum Sepolia",
    11155420: "OP Sepolia",
    1337: "local dev chain",
    31337: "Anvil / Hardhat",
}

# (label, key, width, right-aligned)
BLOCK_COLUMNS = (
    ("block", "block", 9, False),
    ("time", "time", 8, False),
    ("base fee", "base_fee", 10, True),
    ("Δ", "delta", 7, True),
    ("gas used", "gas_used", 17, False),
    ("txs", "txs", 5, True),
    ("events", "events", 6, True),
)


class GasMonitorApp(App[None]):
    """real-time Ethereum gas and event monitor."""

    TITLE = "gas monitor"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (WIDE_LAYOUT_MIN_WIDTH, "-wide")]
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("p", "toggle_pause", "pause/resume"),
        Binding("c", "clear_logs", "clear logs"),
    ]
    CSS = """
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #top {
        height: 1fr;
    }
    #gas {
        width: 1fr;
        min-width: 46;
        border: round $primary;
        padding: 0 1;
    }
    #gas-stats {
        height: auto;
    }
    #gas-spark {
        height: 1fr;
        min-height: 2;
        margin-top: 1;
    }
    #blocks {
        width: auto;
        height: 100%;
        border: round $primary;
    }
    #events {
        height: 1fr;
        border: round $primary;
    }
    /* narrow terminals: gas panel on top, its sparkline beside the numbers */
    Screen.-narrow #top {
        layout: vertical;
        height: 2fr;
    }
    Screen.-narrow #gas {
        layout: horizontal;
        width: 1fr;
        height: 10;
    }
    Screen.-narrow #gas-stats {
        width: 44;
    }
    Screen.-narrow #gas-spark {
        width: 1fr;
        height: 100%;
        margin: 0 0 0 2;
    }
    Screen.-narrow #blocks {
        width: 1fr;
        height: 1fr;
    }
    Screen.-paused #gas, Screen.-paused #blocks, Screen.-paused #events {
        border: round $warning;
    }
    """

    def __init__(self, config: Config, metrics: Metrics, client: EthWsClient) -> None:
        super().__init__()
        self.config = config
        self.metrics = metrics
        self.client = client
        self.paused = False
        self._table_state: tuple[int, int, bool] | None = None  # metrics state the table shows
        self._table_drawn_at = 0.0
        self._gas_version = -1
        self._gas_drawn_at = 0.0
        self._gas_rows = 0
        self._spark_drawn_at = 0.0
        self._feed_drawn_at = 0.0
        self._feed_seq = 0  # metrics.log_seq that the event feed has caught up to
        self._logs_state_seen: LogsState | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="top"):
            with Vertical(id="gas"):
                yield Static(id="gas-stats")
                yield Sparkline([], id="gas-spark")
            yield DataTable(id="blocks", zebra_stripes=True, cursor_type="none", show_cursor=False)
        # min_width=1 renders lines at the real width (cropped with an ellipsis) instead of
        # RichLog's default 78 columns, which would add a horizontal scrollbar on small terminals
        yield RichLog(id="events", max_lines=FEED_MAX_LINES, min_width=1)
        yield Footer()

    def on_mount(self) -> None:
        gas = self.query_one("#gas", Vertical)
        gas.border_title = "gas & network"
        gas.border_subtitle = "base fee history"
        table = self.query_one("#blocks", DataTable)
        table.border_title = "recent blocks"
        for label, key, width, right in BLOCK_COLUMNS:
            table.add_column(Text(label, justify="right" if right else "left"), key=key, width=width)
        feed = self.query_one("#events", RichLog)
        feed.border_title = "live events"
        feed.border_subtitle = self._feed_scope()
        self.run_worker(self.client.run(), name="ws-client", group="network")
        self.set_interval(REFRESH_INTERVAL, self.update_view)
        self.update_view()

    # --- actions -----------------------------------------------------------------

    def action_toggle_pause(self) -> None:
        # data keeps flowing into metrics while paused; only redraws stop
        self.paused = not self.paused
        self.screen.set_class(self.paused, "-paused")
        # catch up immediately on resume
        self._table_drawn_at = self._gas_drawn_at = self._spark_drawn_at = self._feed_drawn_at = 0.0
        self.update_view()

    def action_clear_logs(self) -> None:
        self.query_one("#events", RichLog).clear()
        self._feed_seq = self.metrics.log_seq

    async def action_quit(self) -> None:
        await self.client.stop()
        self.exit()

    # --- rendering ---------------------------------------------------------------

    def update_view(self) -> None:
        """redraw from the current metrics; runs on a timer, independent of data arrival."""
        now = time.monotonic()
        stats = self.metrics.stats()
        self._render_status(stats, now)
        if self.paused:
            return
        # only redraw what changed, and the costlier widgets no more often than needed
        since_gas = now - self._gas_drawn_at
        if since_gas >= GAS_IDLE_INTERVAL or (
            since_gas >= GAS_MIN_INTERVAL and self.metrics.version != self._gas_version
        ):
            self._render_gas(stats, now)
            self._gas_version, self._gas_drawn_at = self.metrics.version, now
        # the busy flag is part of the state so "…" (pending) cells clear once a stream goes quiet
        table_state = (self.metrics.blocks_version, self.metrics.log_seq, self.metrics.log_stream_busy())
        if table_state != self._table_state:
            events_only = self._table_state is not None and table_state[0] == self._table_state[0]
            interval = TABLE_EVENTS_INTERVAL if events_only else TABLE_MIN_INTERVAL
            if now - self._table_drawn_at >= interval and self._render_blocks():
                self._table_state, self._table_drawn_at = table_state, now
        if now - self._feed_drawn_at >= FEED_MIN_INTERVAL:
            self._render_feed()
            self._feed_drawn_at = now

    def _render_status(self, stats: Stats, now: float) -> None:
        status = self.client.status
        left = Text()
        if status.state is ConnectionState.CONNECTED:
            left.append("● connected", style="bold green")
        elif status.state is ConnectionState.RECONNECTING:
            wait = max(0, math.ceil((status.retry_at or now) - now))
            left.append(f"● reconnecting in {wait}s", style="bold red")
        elif status.state is ConnectionState.CONNECTING:
            left.append("◌ connecting…", style="bold yellow")
        else:
            left.append("○ stopped", style="dim")
        if status.state is not ConnectionState.CONNECTED:
            if status.attempt > 1:
                left.append(f" (attempt {status.attempt})", style="dim")
            if status.last_error:
                left.append(f" · {status.last_error}", style="red")
        left.append("  │  ", style="dim")
        left.append(_chain_label(status.chain_id))
        left.append("  │  ", style="dim")
        left.append(status.endpoint, style="dim")

        right = Text()
        latest = self.metrics.latest
        if latest is not None:
            right.append(f"block #{latest.number}", style="bold")
        if self.metrics.last_block_at is not None:
            age = now - self.metrics.last_block_at
            right.append(f"  {format_seconds(age)} ago", style=_age_style(age, stats.block_time))
        if self.paused:
            right.append("  ")
            right.append(" paused ", style="bold black on yellow")

        bar = Table.grid(expand=True, padding=(0, 2), pad_edge=False)
        bar.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        bar.add_column(no_wrap=True, justify="right")
        bar.add_row(left, right)
        # the bar is always one row, so skip the (expensive) full layout pass
        self.query_one("#status", Static).update(bar, layout=False)

    def _render_gas(self, stats: Stats, now: float) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(no_wrap=True, overflow="ellipsis")
        if stats.blocks == 0:
            grid.add_row("", Text("waiting for blocks…", style="dim italic"))
        else:
            if stats.base_fee is None:
                grid.add_row("base fee", Text("n/a (no EIP-1559 base fee)", style="dim"))
            else:
                grid.add_row("base fee", Text.assemble((format_gwei(stats.base_fee), "bold"), " Gwei"))
                grid.add_row("trend", _trend_text(stats))
                fees = (stats.min_base_fee, stats.avg_base_fee, stats.max_base_fee)
                grid.add_row("min/avg/max", " / ".join(format_gwei(fee) for fee in fees))
            grid.add_row(
                "gas used",
                f"{format_percent(stats.utilization)} · avg {format_percent(stats.avg_utilization)}",
            )
            # timestamps have one-second resolution, so sub-second blocks often share one
            last = "<1s" if stats.last_block_time == 0 else format_seconds(stats.last_block_time)
            grid.add_row("block time", f"{format_seconds(stats.block_time)} avg · last {last}")
            grid.add_row(
                "throughput",
                f"{format_count(stats.tps)} tx/s · {format_count(stats.tx_per_block)} tx/block",
            )
            grid.add_row("events", self._events_text(stats))
            grid.add_row("window", f"{stats.blocks}/{self.metrics.window} blocks")
        # a layout pass is only needed when the number of rows changes
        self.query_one("#gas-stats", Static).update(grid, layout=grid.row_count != self._gas_rows)
        self._gas_rows = grid.row_count
        if now - self._spark_drawn_at >= SPARKLINE_MIN_INTERVAL:
            self.query_one("#gas-spark", Sparkline).data = _sparkline_data(stats.base_fee_history)
            self._spark_drawn_at = now

    def _events_text(self, stats: Stats) -> Text:
        logs = self.client.status.logs
        if logs is LogsState.OFF:
            return Text("off", style="dim")
        if logs is LogsState.UNAVAILABLE:
            return Text("unavailable on this node", style="yellow")
        return Text(f"{format_rate(stats.event_rate)} · {format_count(stats.events_per_block)}/block")

    def _render_blocks(self) -> bool:
        """rebuild the blocks table; returns False while held because the user scrolled down."""
        table = self.query_one("#blocks", DataTable)
        if table.scroll_y > 0:
            # rebuilding resets the scroll position, so wait until the user is back at the top
            table.border_subtitle = "scrolled · updates resume at the top"
            return False
        table.border_subtitle = "base fee in Gwei"
        blocks = self.metrics.blocks()
        by_number = {block.number: block for block in blocks}
        table.clear()
        table.add_rows(self._block_row(block, by_number.get(block.number - 1)) for block in blocks)
        return True

    def _block_row(self, block: Block, previous: Block | None) -> tuple[Text, ...]:
        fee = format_gwei(block.base_fee / WEI_PER_GWEI) if block.base_fee is not None else "—"
        # "…" while data is still on its way, "–" when the logs feed did not cover the block
        if block.tx_count is None:
            txs = Text("…", style="dim", justify="right")
        else:
            txs = Text(f"{block.tx_count:,}", justify="right")
        events = self.metrics.event_count(block)
        if events is None:
            events_cell = Text("–", style="dim", justify="right")
        elif self.metrics.events_pending(block):
            events_cell = Text("…", style="dim", justify="right")
        else:
            events_cell = Text(f"{events:,}", justify="right")
        return (
            Text(str(block.number), style="bold cyan"),
            Text(_clock_time(block.timestamp), style="dim"),
            Text(fee, justify="right"),
            _fee_delta(block, previous),
            utilization_bar(block.utilization),
            txs,
            events_cell,
        )

    def _render_feed(self) -> None:
        feed = self.query_one("#events", RichLog)
        self._write_logs_notice(feed)
        new = self.metrics.log_seq - self._feed_seq
        if new <= 0:
            return
        self._feed_seq = self.metrics.log_seq
        shown = self.metrics.recent_logs(min(new, FEED_LINES_PER_REFRESH))
        if new > len(shown):
            feed.write(Text(f"{'':10}  ⋯ {new - len(shown):,} more events not shown", style="dim"))
        compact = feed.scrollable_content_region.width < FEED_FULL_ADDRESS_MIN_WIDTH
        for log in shown:
            feed.write(_feed_line(log, compact=compact))

    def _write_logs_notice(self, feed: RichLog) -> None:
        status = self.client.status
        if status.logs is self._logs_state_seen:
            return
        self._logs_state_seen = status.logs
        if status.logs is LogsState.ACTIVE:
            feed.write(Text(f"· subscribed to logs from {self._feed_scope()}", style="dim italic"))
        elif status.logs is LogsState.UNAVAILABLE:
            reason = status.logs_error or "unknown error"
            feed.write(Text(f"· the node rejected the logs subscription: {reason}", style="yellow"))
        elif status.logs is LogsState.OFF:
            feed.write(Text("· logs subscription disabled", style="dim italic"))

    def _feed_scope(self) -> str:
        if not self.config.subscribe_logs:
            return "logs off"
        addresses = self.config.contract_addresses
        if not addresses:
            return "all contracts, sampled when busy"
        if len(addresses) == 1:
            return to_checksum_address(addresses[0])
        return f"{len(addresses)} contracts"


# --- formatting helpers -------------------------------------------------------------


def format_gwei(value: float | None) -> str:
    """format Gwei with ~4 significant digits, from L2 fees (0.001) to mainnet spikes (1000+)."""
    if value is None:
        return "—"
    if value <= 0:
        return "0"
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:.1f}"
    if value >= 10:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.3f}"
    decimals = min(9, 2 - math.floor(math.log10(value)))  # 1 wei = 1e-9 Gwei
    return f"{value:.{decimals}f}"


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if float(seconds).is_integer() and seconds < 120:
        return f"{seconds:.0f}s"  # block timestamps have whole-second resolution
    if seconds < 1:
        return f"{max(seconds, 0):.2f}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 120:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_rate(per_second: float | None) -> str:
    if per_second is None:
        return "—"
    if per_second >= 100:
        return f"{per_second:,.0f}/s"
    if per_second >= 1:
        return f"{per_second:.1f}/s"
    return f"{per_second * 60:.1f}/min"


def format_count(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}" if value < 10 else f"{value:,.0f}"


def format_percent(fraction: float | None) -> str:
    return "—" if fraction is None else f"{fraction * 100:.1f}%"


def utilization_bar(fraction: float) -> Text:
    """gas used as a small bar; above 50% (the EIP-1559 target) the next base fee rises."""
    filled = round(min(max(fraction, 0.0), 1.0) * BAR_WIDTH)
    style = "red" if fraction >= 0.9 else "yellow" if fraction > 0.5 else "green"
    return Text.assemble(
        ("█" * filled, style),
        ("░" * (BAR_WIDTH - filled), "dim"),
        f" {fraction * 100:5.1f}%",
    )


def _sparkline_data(history: tuple[float, ...]) -> list[float]:
    """the sparkline scales from min to max, so flatten noise when fees barely move (e.g. L2 floors)."""
    if history and max(history) - min(history) <= SPARKLINE_FLAT_RANGE * max(history):
        return [history[-1]] * len(history)
    return list(history)


def _fee_delta(block: Block, previous: Block | None) -> Text:
    """base fee change against the parent block."""
    if previous is None or block.base_fee is None or not previous.base_fee:
        return Text("", justify="right")
    change = (block.base_fee - previous.base_fee) / previous.base_fee
    if abs(change) < 0.0005:
        return Text("0.0%", style="dim", justify="right")
    return Text(f"{change * 100:+.1f}%", style="red" if change > 0 else "green", justify="right")


def _trend_text(stats: Stats) -> Text:
    if stats.trend_change is None:
        return Text("—", style="dim")
    span = f"{stats.trend_change * 100:+.1f}% ({min(stats.blocks, TREND_BLOCKS)} blocks)"
    if stats.trend == "rising":
        return Text(f"▲ rising {span}", style="red")
    if stats.trend == "falling":
        return Text(f"▼ falling {span}", style="green")
    return Text(f"◆ stable {span}", style="dim")


def _feed_line(log: LogEvent, *, compact: bool) -> Text:
    decoded = decode_log(log.topics, log.data)
    line = Text(no_wrap=True, overflow="ellipsis")
    block = "pending" if log.block_number is None else str(log.block_number)
    line.append(f"{block:>10}", style="cyan")
    line.append("  ")
    line.append(short_address(log.address) if compact else to_checksum_address(log.address))
    line.append("  ")
    line.append(f"{short_hex(log.topics[0]) if log.topics else 'anonymous':<15}", style="dim")
    line.append("  ")
    line.append(f"{decoded.name or 'unknown':<16}", style="bold magenta" if decoded.name else "dim")
    for label, value in decoded.args:
        line.append(f" {label}=", style="dim")
        line.append(value)
    if log.removed:
        line.append("  removed by reorg", style="bold red")
    return line


def _chain_label(chain_id: int | None) -> str:
    if chain_id is None:
        return "chain ?"
    name = CHAIN_NAMES.get(chain_id)
    return f"{name} · chain {chain_id}" if name else f"chain {chain_id}"


def _age_style(age: float, block_time: float | None) -> str:
    expected = max(block_time or 12.0, 1.0)
    if age < expected * 2:
        return "green"
    if age < expected * 5:
        return "yellow"
    return "bold red"


def _clock_time(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"
