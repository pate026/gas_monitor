"""asyncio Ethereum JSON-RPC client over a raw WebSocket, with subscriptions and auto-reconnect.

one reader task dispatches both request responses and subscription notifications.
the client subscribes to newHeads (and optionally logs), fetches the few values that
newHeads does not carry, backfills recent blocks after (re)connecting, and exposes a
ConnectionStatus that the UI polls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import ssl
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

logger = logging.getLogger(__name__)

OPEN_TIMEOUT = 15.0  # seconds for TCP + TLS + WebSocket handshake
CLOSE_TIMEOUT = 2.0  # keep quitting snappy even when the server is unresponsive
REQUEST_TIMEOUT = 10.0
MAX_MESSAGE_SIZE = 16 * 2**20  # full blocks with many transaction hashes can exceed the 1 MiB default
BACKFILL_CONCURRENCY = 4  # parallel eth_getBlockByNumber calls, gentle on rate-limited providers
MAX_BACKGROUND_TASKS = 64  # tx count lookups are skipped beyond this to avoid piling up requests
TX_COUNT_RETRY_DELAYS = (0.0, 0.5, 1.5)  # load-balanced providers may not know a fresh block yet


@dataclass(slots=True)
class Block:
    number: int
    hash: str
    timestamp: int  # unix seconds from the block header
    base_fee: int | None  # wei; None on chains without EIP-1559
    gas_used: int
    gas_limit: int
    received_at: float  # local monotonic clock
    tx_count: int | None = None  # newHeads carries no transactions, so this is filled in later
    events_tracked: bool = False  # whether the logs subscription was active for this block

    @property
    def utilization(self) -> float:
        return self.gas_used / self.gas_limit if self.gas_limit else 0.0


@dataclass(frozen=True, slots=True)
class LogEvent:
    block_number: int | None
    block_hash: str | None
    tx_hash: str | None
    log_index: int | None
    address: str
    topics: tuple[str, ...]
    data: str
    removed: bool  # true when a chain reorganization dropped the log


class EventSink(Protocol):
    """receiver of parsed chain data; metrics.Metrics implements it."""

    def on_block(self, block: Block, live: bool) -> None: ...
    def on_tx_count(self, block_hash: str, count: int) -> None: ...
    def on_log(self, log: LogEvent) -> None: ...
    def on_logs_subscription(self, active: bool) -> None: ...


class ConnectionState(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


class LogsState(str, Enum):
    OFF = "off"  # disabled by configuration
    PENDING = "pending"  # waiting for a connection
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"  # the node rejected the subscription


@dataclass(slots=True)
class ConnectionStatus:
    """connection details for display; updated in place by the client."""

    endpoint: str  # redacted, safe to show
    state: ConnectionState = ConnectionState.CONNECTING
    chain_id: int | None = None
    attempt: int = 1  # current connection attempt since the last healthy session
    last_error: str | None = None
    retry_at: float | None = None  # monotonic time of the next attempt while reconnecting
    connected_at: float | None = None
    logs: LogsState = LogsState.OFF
    logs_error: str | None = None


class RpcError(Exception):
    """error object returned by the node for a JSON-RPC request."""

    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(message if code is None else f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data

    @classmethod
    def from_payload(cls, payload: Any) -> RpcError:
        if isinstance(payload, dict):
            message = str(payload.get("message") or "unknown error")
            return cls(payload.get("code"), message, payload.get("data"))
        return cls(None, str(payload))


class StaleSubscriptionError(Exception):
    """raised when the connection looks alive but no new blocks arrive."""


# errors a single request can fail with; background work treats them as "skip this one"
_REQUEST_ERRORS = (RpcError, ConnectionError, ConnectionClosed, asyncio.TimeoutError)

_HTTP_HINTS = {401: " (check the API key)", 403: " (check the API key)", 429: " (rate limited)"}


@dataclass(slots=True)
class _PendingRequest:
    future: asyncio.Future[Any]
    subscription: str | None  # subscription kind when this is an eth_subscribe call


class EthWsClient:
    """keeps one WebSocket connection alive and streams parsed blocks and logs into a sink."""

    def __init__(
        self,
        url: str,
        sink: EventSink,
        *,
        log_addresses: Sequence[str] = (),
        subscribe_logs: bool = True,
        backfill_blocks: int = 50,
        stale_timeout: float = 60.0,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        self._url = url
        self._sink = sink
        self._log_addresses = tuple(log_addresses)
        self._subscribe_logs = subscribe_logs
        self._backfill_blocks = backfill_blocks
        self._stale_timeout = stale_timeout
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self.status = ConnectionStatus(
            endpoint=redact_url(url),
            logs=LogsState.PENDING if subscribe_logs else LogsState.OFF,
        )
        self._stop = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._next_id = 0
        self._pending: dict[int, _PendingRequest] = {}
        self._subscriptions: dict[str, str] = {}  # subscription id -> "newHeads" | "logs"
        self._tasks: set[asyncio.Task[None]] = set()
        self._logs_active = False
        self._healthy = False  # set once the current session delivers a block
        self._last_head_at = 0.0
        self._highest_block: int | None = None

    # --- lifecycle ------------------------------------------------------------

    async def run(self) -> None:
        """connect, subscribe and stream until stop() is called, reconnecting on failure."""
        failures = 0
        try:
            while not self._stop.is_set():
                self._set_state(ConnectionState.CONNECTING, attempt=failures + 1)
                self._healthy = False  # reset here: a failed connect() never reaches _session()
                error = "connection closed"
                try:
                    async with connect(
                        self._url,
                        open_timeout=OPEN_TIMEOUT,
                        close_timeout=CLOSE_TIMEOUT,
                        max_size=MAX_MESSAGE_SIZE,
                    ) as ws:
                        await self._session(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # every failure ends in a reconnect, so catch broadly
                    error = self._describe_error(exc)
                if self._stop.is_set():
                    break
                # only reset the backoff after a session that actually streamed blocks, so a
                # server that accepts and immediately drops connections cannot cause a tight loop
                failures = 0 if self._healthy else failures + 1
                delay = self._backoff_delay(failures)
                logger.warning("disconnected (%s), reconnecting in %.1fs", error, delay)
                self._set_state(
                    ConnectionState.RECONNECTING,
                    attempt=failures + 1,
                    error=error,
                    retry_at=time.monotonic() + delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._set_state(ConnectionState.STOPPED)  # also when the task is cancelled

    async def stop(self) -> None:
        """stop streaming and close the connection."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            await ws.close()

    async def _session(self, ws: ClientConnection) -> None:
        """run one connection: handshake and subscriptions, then stream until it drops."""
        if self._stop.is_set():
            return
        self._ws = ws
        self._last_head_at = time.monotonic()
        known_highest = self._highest_block
        reader = asyncio.create_task(self._read_loop(ws), name="rpc-reader")
        watched: set[asyncio.Task[None]] = {reader}
        try:
            await self._handshake()
            self._set_state(ConnectionState.CONNECTED)
            logger.info("connected to %s (chain id %s)", self.status.endpoint, self.status.chain_id)
            if self._backfill_blocks > 0:
                self._spawn(self._backfill(known_highest))
            if self._stale_timeout > 0:
                watched.add(asyncio.create_task(self._watch_stale(), name="stale-watchdog"))
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raises a network error or a stale subscription
            raise ConnectionError(_close_description(ws))  # the server closed the connection cleanly
        finally:
            leftovers = (*watched, *self._tasks)
            for task in leftovers:
                task.cancel()
            await asyncio.gather(*leftovers, return_exceptions=True)
            self._ws = None
            self._subscriptions.clear()
            self._fail_pending(ConnectionError("connection closed"))
            if self._logs_active:
                self._logs_active = False
                self._sink.on_logs_subscription(False)
            if self._subscribe_logs:
                self.status.logs = LogsState.PENDING

    async def _handshake(self) -> None:
        """identify the chain, then subscribe to logs (if enabled) and newHeads."""
        try:
            self.status.chain_id = _to_int(await self._request("eth_chainId"))
        except RpcError as exc:
            logger.warning("eth_chainId failed: %s", exc)
        if self._subscribe_logs:
            # logs first, so every live block that follows has complete event counts
            log_filter: dict[str, Any] = {}
            if self._log_addresses:
                addresses = list(self._log_addresses)
                log_filter["address"] = addresses[0] if len(addresses) == 1 else addresses
            try:
                await self._request("eth_subscribe", ["logs", log_filter], subscription="logs")
            except RpcError as exc:
                self.status.logs = LogsState.UNAVAILABLE
                self.status.logs_error = _lower_first(exc.message)
                logger.warning("logs subscription rejected: %s", exc)
        await self._request("eth_subscribe", ["newHeads"], subscription="newHeads")

    # --- JSON-RPC plumbing ------------------------------------------------------

    async def _request(
        self, method: str, params: list[Any] | None = None, *, subscription: str | None = None
    ) -> Any:
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(future, subscription)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or []}
        try:
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                # when the caller is cancelled right after the reader failed this request,
                # mark the error as seen so asyncio does not log "exception never retrieved"
                future.exception()

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for message in ws:
                self._handle_message(message)
        finally:
            self._fail_pending(ConnectionError(_close_description(ws)))

    def _handle_message(self, message: str | bytes) -> None:
        try:
            payload = json.loads(message)
        except ValueError:
            logger.warning("ignoring a non-JSON message of %d bytes", len(message))
            return
        for item in payload if isinstance(payload, list) else (payload,):
            if not isinstance(item, dict):
                continue
            try:
                self._dispatch(item)
            except Exception:  # one malformed message must not stop the stream
                logger.exception("failed to handle a message")

    def _dispatch(self, message: dict[str, Any]) -> None:
        if message.get("method") == "eth_subscription":
            params = message.get("params") or {}
            kind = self._subscriptions.get(params.get("subscription"))
            if kind == "newHeads":
                self._on_head(params["result"])
            elif kind == "logs":
                self._sink.on_log(parse_log(params["result"]))
            return
        pending = self._pending.pop(message.get("id"), None)
        if pending is None or pending.future.done():
            # unknown id, or the request already timed out
            if message.get("error") is not None:
                logger.warning("node error without a pending request: %s", message["error"])
            return
        if message.get("error") is not None:
            pending.future.set_exception(RpcError.from_payload(message["error"]))
            return
        result = message.get("result")
        if pending.subscription and isinstance(result, str):
            # register right here rather than in the awaiting coroutine, because the first
            # notification can arrive in the same read burst as this response
            self._subscriptions[result] = pending.subscription
            if pending.subscription == "logs":
                self._activate_logs()
        pending.future.set_result(result)

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for request in pending.values():
            if not request.future.done():
                request.future.set_exception(error)

    # --- stream handlers --------------------------------------------------------

    def _activate_logs(self) -> None:
        self._logs_active = True
        self.status.logs = LogsState.ACTIVE
        self.status.logs_error = None
        self._sink.on_logs_subscription(True)

    def _on_head(self, raw: dict[str, Any]) -> None:
        now = time.monotonic()
        block = parse_block(raw, received_at=now, events_tracked=self._logs_active)
        self._last_head_at = now
        was_healthy, previous_highest = self._healthy, self._highest_block
        if not self._healthy:
            self._healthy = True
            self.status.attempt = 1
        self._emit_block(block, live=True)
        if len(self._tasks) >= MAX_BACKGROUND_TASKS:
            return
        self._spawn(self._fetch_tx_count(block.hash))
        # providers occasionally skip head notifications under load; fetch what is missing
        # (the first head of a session is covered by the session backfill instead)
        if was_healthy and previous_highest is not None and block.number > previous_highest + 1:
            first = max(previous_highest + 1, block.number - self._backfill_blocks + 1)
            if first < block.number:
                missing = range(block.number - 1, first - 1, -1)
                logger.info("newHeads skipped %d block(s) before %d, fetching them", len(missing), block.number)
                self._spawn(self._fetch_blocks(missing, events_tracked=self._logs_active))

    def _emit_block(self, block: Block, *, live: bool) -> None:
        if self._highest_block is None or block.number > self._highest_block:
            self._highest_block = block.number
        self._sink.on_block(block, live)

    async def _fetch_tx_count(self, block_hash: str) -> None:
        """newHeads carries no transactions, so ask for the count separately."""
        for delay in TX_COUNT_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self._request("eth_getBlockTransactionCountByHash", [block_hash])
            except _REQUEST_ERRORS as exc:
                logger.debug("tx count for %s unavailable: %s", block_hash, exc)
                return
            if result is not None:
                self._sink.on_tx_count(block_hash, _to_int(result))
                return

    async def _backfill(self, known_highest: int | None) -> None:
        """load recent blocks right after connecting so the rolling stats fill immediately."""
        try:
            latest = await self._request("eth_getBlockByNumber", ["latest", False])
        except _REQUEST_ERRORS as exc:
            logger.info("backfill skipped: %s", exc)
            return
        if not isinstance(latest, dict):
            return
        head = _block_from_body(latest)
        self._emit_block(head, live=False)
        lowest = head.number - self._backfill_blocks + 1
        if known_highest is not None:
            lowest = max(lowest, known_highest + 1)  # after a reconnect only fill the gap
        # newest first, so the top of the blocks table fills in first
        await self._fetch_blocks(range(head.number - 1, max(lowest, 0) - 1, -1))

    async def _fetch_blocks(self, numbers: range, *, events_tracked: bool = False) -> None:
        """fetch block bodies by number with limited concurrency and emit them as backfill."""
        semaphore = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        async def fetch(number: int) -> None:
            async with semaphore:
                try:
                    body = await self._request("eth_getBlockByNumber", [hex(number), False])
                except _REQUEST_ERRORS as exc:
                    logger.debug("fetching block %d failed: %s", number, exc)
                    return
            if isinstance(body, dict):
                self._emit_block(_block_from_body(body, events_tracked=events_tracked), live=False)

        await asyncio.gather(*(fetch(number) for number in numbers))

    async def _watch_stale(self) -> None:
        interval = min(5.0, self._stale_timeout / 2)
        while True:
            await asyncio.sleep(interval)
            idle = time.monotonic() - self._last_head_at
            if idle >= self._stale_timeout:
                raise StaleSubscriptionError(f"no new block for {idle:.0f}s")

    # --- helpers ----------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("background task failed", exc_info=task.exception())

    def _set_state(
        self,
        state: ConnectionState,
        *,
        attempt: int | None = None,
        error: str | None = None,
        retry_at: float | None = None,
    ) -> None:
        status = self.status
        status.state = state
        status.retry_at = retry_at
        if attempt is not None:
            status.attempt = attempt
        if error is not None:
            status.last_error = error
        if state is ConnectionState.CONNECTED:
            status.connected_at = time.monotonic()
            status.last_error = None

    def _backoff_delay(self, failures: int) -> float:
        """exponential backoff with jitter: ~1s, 1s, 2s, 4s, ... capped at max_backoff."""
        delay = self._min_backoff * 2 ** max(failures - 1, 0)
        return min(self._max_backoff, delay * random.uniform(0.8, 1.2))

    def _describe_error(self, exc: BaseException) -> str:
        if isinstance(exc, InvalidStatus):
            code = exc.response.status_code
            text = f"HTTP {code} during WebSocket handshake{_HTTP_HINTS.get(code, '')}"
        elif isinstance(exc, ConnectionClosed):
            frame = exc.rcvd or exc.sent
            text = _close_text(frame.code, frame.reason) if frame else "connection lost"
        elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            text = "timed out"
        elif isinstance(exc, ssl.SSLCertVerificationError):
            text = f"TLS certificate verification failed ({exc.verify_message})"
        elif isinstance(exc, ssl.SSLError):
            text = f"TLS error ({exc.reason or exc})"
        elif isinstance(exc, socket.gaierror):
            text = f"cannot resolve host ({exc.strerror})"
        elif isinstance(exc, OSError) and exc.errno and exc.errno > 0:
            text = os.strerror(exc.errno)  # e.g. "Connection refused" rather than asyncio's wording
        elif isinstance(exc, OSError):
            text = exc.strerror or str(exc) or type(exc).__name__
        else:
            text = str(exc) or type(exc).__name__
        # never surface the raw URL, which usually embeds an API key
        return _lower_first(text.replace(self._url, self.status.endpoint))


# --- parsing ----------------------------------------------------------------------


def _to_int(value: Any) -> int:
    """decode a JSON-RPC quantity such as "0x1b4"."""
    return value if isinstance(value, int) else int(value, 16)


def _optional_int(value: Any) -> int | None:
    return None if value is None else _to_int(value)


def parse_block(
    raw: dict[str, Any],
    *,
    received_at: float,
    tx_count: int | None = None,
    events_tracked: bool = False,
) -> Block:
    """build a Block from a newHeads payload or an eth_getBlockByNumber result."""
    return Block(
        number=_to_int(raw["number"]),
        hash=raw["hash"],
        timestamp=_to_int(raw["timestamp"]),
        base_fee=_optional_int(raw.get("baseFeePerGas")),
        gas_used=_to_int(raw["gasUsed"]),
        gas_limit=_to_int(raw["gasLimit"]),
        received_at=received_at,
        tx_count=tx_count,
        events_tracked=events_tracked,
    )


def _block_from_body(body: dict[str, Any], *, events_tracked: bool = False) -> Block:
    transactions = body.get("transactions")
    return parse_block(
        body,
        received_at=time.monotonic(),
        tx_count=len(transactions) if isinstance(transactions, list) else None,
        events_tracked=events_tracked,
    )


def parse_log(raw: dict[str, Any]) -> LogEvent:
    """build a LogEvent from a logs subscription payload."""
    return LogEvent(
        block_number=_optional_int(raw.get("blockNumber")),
        block_hash=raw.get("blockHash"),
        tx_hash=raw.get("transactionHash"),
        log_index=_optional_int(raw.get("logIndex")),
        address=raw.get("address") or "",
        topics=tuple(raw.get("topics") or ()),
        data=raw.get("data") or "0x",
        removed=bool(raw.get("removed", False)),
    )


def redact_url(url: str) -> str:
    """drop credentials, path and query (where providers put API keys) before display."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = parts.port
    except ValueError:
        return "<invalid URL>"
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    netloc = f"{host}:{port}" if port else host
    hidden = parts.path.strip("/") or parts.query or parts.username or parts.password
    return f"{parts.scheme}://{netloc}{'/…' if hidden else ''}"


def _close_text(code: int | None, reason: str | None) -> str:
    if code is None:
        return "connection lost"
    return f"connection closed (code {code}: {reason})" if reason else f"connection closed (code {code})"


def _close_description(ws: ClientConnection) -> str:
    return _close_text(ws.close_code, ws.close_reason)


def _lower_first(text: str) -> str:
    """lowercase a leading capital (e.g. from OS errors) but keep acronyms such as HTTP or I/O."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text
