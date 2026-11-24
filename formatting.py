"""ui-independent formatting helpers shared by the terminal UI and the desktop GUI."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from config import Config
from decoder import decode_log, short_address, short_hex, to_checksum_address
from metrics import TREND_BLOCKS, Stats
from ws_client import ConnectionStatus, LogEvent, LogsState

# semantic colors; each UI maps them to its own styles
Level = Literal["green", "yellow", "red"]

FEE_CHANGE_FLAT = 0.0005  # relative base fee change shown as a plain 0.0%
UTILIZATION_HIGH = 0.9  # gas used at or above this is shown red
UTILIZATION_TARGET = 0.5  # the EIP-1559 target: above it the next base fee rises
DEFAULT_BLOCK_TIME = 12.0  # assumed block time before the window has one
FEED_BLOCK_WIDTH = 10
FEED_TOPIC_WIDTH = 15
FEED_NAME_WIDTH = 16

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


def chain_label(chain_id: int | None) -> str:
    if chain_id is None:
        return "chain ?"
    name = CHAIN_NAMES.get(chain_id)
    return f"{name} · chain {chain_id}" if name else f"chain {chain_id}"


def clock_time(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"


def utilization_level(fraction: float) -> Level:
    """gas used color; above 50% (the EIP-1559 target) the next base fee rises."""
    if fraction >= UTILIZATION_HIGH:
        return "red"
    return "yellow" if fraction > UTILIZATION_TARGET else "green"


def age_level(age: float, block_time: float | None) -> Level:
    """color for the time since the last block, relative to the expected block time."""
    expected = max(block_time or DEFAULT_BLOCK_TIME, 1.0)
    if age < expected * 2:
        return "green"
    if age < expected * 5:
        return "yellow"
    return "red"


def fee_change(base_fee: int | None, previous_base_fee: int | None) -> float | None:
    """relative base fee change against the parent block, None when it cannot be computed."""
    if base_fee is None or not previous_base_fee:
        return None
    return (base_fee - previous_base_fee) / previous_base_fee


def trend_summary(stats: Stats) -> tuple[str, Level | None] | None:
    """base fee trend as text and color (None = neutral), or None before there is a trend."""
    if stats.trend_change is None:
        return None
    span = f"{stats.trend_change * 100:+.1f}% ({min(stats.blocks, TREND_BLOCKS)} blocks)"
    if stats.trend == "rising":
        return f"▲ rising {span}", "red"
    if stats.trend == "falling":
        return f"▼ falling {span}", "green"
    return f"◆ stable {span}", None


@dataclass(frozen=True, slots=True)
class FeedLine:
    """one event feed line as padded text fields; each UI styles and joins them."""

    block: str
    address: str
    topic: str
    name: str
    known: bool  # whether the event signature was recognized
    args: tuple[tuple[str, str], ...]
    removed: bool  # dropped by a chain reorganization


def feed_line(log: LogEvent, *, compact: bool) -> FeedLine:
    """decode a log into feed fields; compact shortens the contract address."""
    decoded = decode_log(log.topics, log.data)
    block = "pending" if log.block_number is None else str(log.block_number)
    topic = short_hex(log.topics[0]) if log.topics else "anonymous"
    return FeedLine(
        block=f"{block:>{FEED_BLOCK_WIDTH}}",
        address=short_address(log.address) if compact else to_checksum_address(log.address),
        topic=f"{topic:<{FEED_TOPIC_WIDTH}}",
        name=f"{decoded.name or 'unknown':<{FEED_NAME_WIDTH}}",
        known=decoded.name is not None,
        args=tuple(decoded.args),
        removed=log.removed,
    )


def more_events_text(hidden: int) -> str:
    """placeholder line for events skipped because the feed shows only the newest ones."""
    return f"{'':{FEED_BLOCK_WIDTH}}  ⋯ {hidden:,} more events not shown"


def feed_scope(config: Config) -> str:
    """which contracts the feed covers."""
    if not config.subscribe_logs:
        return "logs off"
    addresses = config.contract_addresses
    if not addresses:
        return "all contracts, sampled when busy"
    if len(addresses) == 1:
        return to_checksum_address(addresses[0])
    return f"{len(addresses)} contracts"


def logs_notice(status: ConnectionStatus, scope: str) -> tuple[str, Level | None] | None:
    """feed line announcing a logs subscription state and its color (None = dim italic)."""
    if status.logs is LogsState.ACTIVE:
        return f"· subscribed to logs from {scope}", None
    if status.logs is LogsState.UNAVAILABLE:
        reason = status.logs_error or "unknown error"
        return f"· the node rejected the logs subscription: {reason}", "yellow"
    if status.logs is LogsState.OFF:
        return "· logs subscription disabled", None
    return None
