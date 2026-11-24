"""runtime configuration from CLI flags, environment variables and a .env file (in that priority)."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from decoder import has_valid_checksum, is_address

DEFAULT_WINDOW = 50
DEFAULT_STALE_TIMEOUT = 60.0
MIN_WINDOW = 2
MAX_WINDOW = 1000

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """raised when the configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    ws_url: str
    contract_addresses: tuple[str, ...] = ()  # lowercase; empty means all contracts
    subscribe_logs: bool = True
    window: int = DEFAULT_WINDOW
    stale_timeout: float = DEFAULT_STALE_TIMEOUT  # seconds without a new block before reconnecting
    log_file: Path | None = None
    gui: bool = False  # desktop GUI instead of the terminal UI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="real-time Ethereum block, gas price and event monitor",
        epilog="each flag overrides its environment variable, which overrides the .env file",
    )
    parser.add_argument(
        "-u", "--rpc-url", metavar="URL",
        help="WebSocket JSON-RPC endpoint, ws:// or wss:// (env: WS_RPC_URL)",
    )
    parser.add_argument(
        "-c", "--contract", metavar="ADDRESS",
        help="only stream logs emitted by this contract, comma-separated for several (env: CONTRACT_ADDRESS)",
    )
    parser.add_argument(
        "-w", "--window", type=int, metavar="BLOCKS",
        help=f"rolling window size in blocks (env: WINDOW_SIZE, default: {DEFAULT_WINDOW})",
    )
    parser.add_argument(
        "--logs", action=argparse.BooleanOptionalAction, default=None,
        help="subscribe to contract logs (env: SUBSCRIBE_LOGS, default: on)",
    )
    parser.add_argument(
        "--stale-timeout", type=float, metavar="SECONDS",
        help="reconnect when no new block arrives for this long, 0 disables "
        f"(env: STALE_TIMEOUT, default: {DEFAULT_STALE_TIMEOUT:g})",
    )
    parser.add_argument(
        "--log-file", type=Path, metavar="PATH",
        help="write diagnostic logs to this file (env: LOG_FILE)",
    )
    parser.add_argument(
        "--gui", action=argparse.BooleanOptionalAction, default=None,
        help="open the desktop GUI instead of the terminal UI (env: GUI, default: off)",
    )
    parser.add_argument(
        "--env-file", type=Path, metavar="PATH",
        help="read variables from this file instead of ./.env",
    )
    return parser


def load_config(argv: Sequence[str] | None = None) -> Config:
    """parse CLI flags, load the .env file and validate the merged settings."""
    args = build_parser().parse_args(argv)
    _load_env_file(args.env_file)

    window = args.window if args.window is not None else _env_int("WINDOW_SIZE", DEFAULT_WINDOW)
    if not MIN_WINDOW <= window <= MAX_WINDOW:
        raise ConfigError(f"window must be between {MIN_WINDOW} and {MAX_WINDOW} blocks, got {window}")

    stale_timeout = (
        args.stale_timeout
        if args.stale_timeout is not None
        else _env_float("STALE_TIMEOUT", DEFAULT_STALE_TIMEOUT)
    )
    if stale_timeout < 0:
        raise ConfigError("stale timeout must be zero (disabled) or a positive number of seconds")

    log_file = args.log_file
    if log_file is None and (env_log_file := _env_str("LOG_FILE")):
        log_file = Path(env_log_file)

    return Config(
        ws_url=_validate_ws_url(args.rpc_url or _env_str("WS_RPC_URL")),
        contract_addresses=_parse_addresses(args.contract or _env_str("CONTRACT_ADDRESS")),
        subscribe_logs=args.logs if args.logs is not None else _env_bool("SUBSCRIBE_LOGS", True),
        window=window,
        stale_timeout=stale_timeout,
        log_file=log_file,
        gui=args.gui if args.gui is not None else _env_bool("GUI", False),
    )


def _load_env_file(path: Path | None) -> None:
    # real environment variables always win over values from the file
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"env file not found: {path}")
        load_dotenv(path, override=False)
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().with_name(".env")):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _validate_ws_url(url: str | None) -> str:
    if not url:
        raise ConfigError("no RPC endpoint configured: set WS_RPC_URL in .env or pass --rpc-url")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        raise ConfigError(
            "WS_RPC_URL must be a WebSocket endpoint (ws:// or wss://), not HTTP; "
            "most providers publish a separate wss:// URL"
        )
    try:
        valid = scheme in ("ws", "wss") and bool(parts.hostname)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        valid = False
    if not valid:
        # the URL itself is not echoed because it usually embeds an API key
        raise ConfigError("WS_RPC_URL is not a valid WebSocket URL, expected ws://host or wss://host")
    return url


def _parse_addresses(raw: str | None) -> tuple[str, ...]:
    addresses: list[str] = []
    for item in re.split(r"[\s,]+", (raw or "").strip()):
        if not item:
            continue
        if not is_address(item):
            raise ConfigError(f"invalid contract address {item!r}: expected 0x followed by 40 hex characters")
        if not has_valid_checksum(item):
            raise ConfigError(f"contract address {item} fails its EIP-55 checksum, check it for typos")
        if item.lower() not in addresses:
            addresses.append(item.lower())
    return tuple(addresses)


def _env_str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    value = _env_str(name)
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")
