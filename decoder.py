"""keccak-256, EIP-55 addresses and best-effort decoding of EVM log topics and data.

hashlib.sha3_256 implements NIST SHA-3, whose padding differs from the original
Keccak used by Ethereum. OpenSSL 3.2+ exposes real Keccak-256 through hashlib and is
used when present; otherwise a compact pure-Python fallback keeps the dependency list
minimal (it is ~100x slower, but only hashes signatures at import time and the
addresses that are actually displayed, with caching).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

# --- Keccak-256 --------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_RATE = 136  # sponge rate in bytes for a 256-bit digest: (1600 - 2 * 256) / 8


def _round_constants() -> tuple[int, ...]:
    """derive the 24 iota round constants from the Keccak LFSR (x^8 + x^6 + x^5 + x^4 + 1)."""
    constants = []
    lfsr = 1
    for _ in range(24):
        constant = 0
        for j in range(7):
            if lfsr & 1:
                constant |= 1 << ((1 << j) - 1)
            lfsr = ((lfsr << 1) ^ 0x71) & 0xFF if lfsr & 0x80 else lfsr << 1
        constants.append(constant)
    return tuple(constants)


def _rho_pi_steps() -> tuple[tuple[int, int, int, int], ...]:
    """precompute (source lane, source column, target lane, rotation) for rho and pi."""
    rotation = {(0, 0): 0}
    x, y = 1, 0
    for t in range(24):
        rotation[(x, y)] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    return tuple(
        (x + 5 * y, x, y + 5 * ((2 * x + 3 * y) % 5), rotation[(x, y)])
        for y in range(5)
        for x in range(5)
    )


_ROUND_CONSTANTS = _round_constants()
_RHO_PI = _rho_pi_steps()


def _keccak_f1600(lanes: list[int]) -> None:
    """apply the 24-round Keccak-f[1600] permutation in place (lane index = x + 5 * y)."""
    mask = _MASK64
    b = [0] * 25
    for constant in _ROUND_CONSTANTS:
        # theta: column parities, applied to each lane while it is moved below
        c0 = lanes[0] ^ lanes[5] ^ lanes[10] ^ lanes[15] ^ lanes[20]
        c1 = lanes[1] ^ lanes[6] ^ lanes[11] ^ lanes[16] ^ lanes[21]
        c2 = lanes[2] ^ lanes[7] ^ lanes[12] ^ lanes[17] ^ lanes[22]
        c3 = lanes[3] ^ lanes[8] ^ lanes[13] ^ lanes[18] ^ lanes[23]
        c4 = lanes[4] ^ lanes[9] ^ lanes[14] ^ lanes[19] ^ lanes[24]
        d = (
            c4 ^ (((c1 << 1) | (c1 >> 63)) & mask),
            c0 ^ (((c2 << 1) | (c2 >> 63)) & mask),
            c1 ^ (((c3 << 1) | (c3 >> 63)) & mask),
            c2 ^ (((c4 << 1) | (c4 >> 63)) & mask),
            c3 ^ (((c0 << 1) | (c0 >> 63)) & mask),
        )
        # rho and pi: rotate each lane and move it to its new position
        for source, column, target, shift in _RHO_PI:
            lane = lanes[source] ^ d[column]
            b[target] = ((lane << shift) | (lane >> (64 - shift))) & mask
        # chi: non-linear mixing along each row
        for row in (0, 5, 10, 15, 20):
            b0, b1, b2, b3, b4 = b[row : row + 5]
            lanes[row] = b0 ^ (~b1 & b2)
            lanes[row + 1] = b1 ^ (~b2 & b3)
            lanes[row + 2] = b2 ^ (~b3 & b4)
            lanes[row + 3] = b3 ^ (~b4 & b0)
            lanes[row + 4] = b4 ^ (~b0 & b1)
        # iota: break the symmetry between rounds
        lanes[0] ^= constant


def _keccak256_python(data: bytes) -> bytes:
    padded = bytearray(data)
    padded.append(0x01)
    padded.extend(bytes(-len(padded) % _RATE))
    padded[-1] |= 0x80
    lanes = [0] * 25
    for start in range(0, len(padded), _RATE):
        for i in range(_RATE // 8):
            offset = start + 8 * i
            lanes[i] ^= int.from_bytes(padded[offset : offset + 8], "little")
        _keccak_f1600(lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


_EMPTY_KECCAK = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def _openssl_keccak256() -> Callable[[bytes], bytes] | None:
    """return OpenSSL's Keccak-256 when this Python links OpenSSL 3.2+, verified on a known vector."""
    try:
        if hashlib.new("KECCAK-256", b"").hexdigest() != _EMPTY_KECCAK:
            return None
    except (ValueError, TypeError):
        return None
    return lambda data: hashlib.new("KECCAK-256", data).digest()


_keccak256_impl = _openssl_keccak256() or _keccak256_python


def keccak256(data: bytes) -> bytes:
    """return the Keccak-256 digest used by Ethereum (not NIST SHA3-256)."""
    return _keccak256_impl(data)


# --- addresses and display helpers -------------------------------------------

_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_MAX_UINT256 = (1 << 256) - 1


def is_address(value: str) -> bool:
    """check for a 0x-prefixed 20-byte hex address (any letter case)."""
    return _ADDRESS_RE.fullmatch(value) is not None


@lru_cache(maxsize=4096)
def to_checksum_address(address: str) -> str:
    """format an address with its EIP-55 mixed-case checksum; other strings pass through."""
    if not is_address(address):
        return address
    body = address[2:].lower()
    digest = keccak256(body.encode("ascii")).hex()
    # the 40 address characters take their casing from the first 40 of the 64 digest nibbles
    return "0x" + "".join(
        char.upper() if int(nibble, 16) >= 8 else char for char, nibble in zip(body, digest, strict=False)
    )


def has_valid_checksum(address: str) -> bool:
    """validate EIP-55 casing; all-lowercase or all-uppercase addresses carry no checksum."""
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    return to_checksum_address(address) == address


def short_hex(value: str, head: int = 8, tail: int = 4) -> str:
    """shorten a long hex string such as a hash: 0xddf252ad…b3ef."""
    if len(value) <= 2 + head + tail + 1:
        return value
    return f"{value[: 2 + head]}…{value[-tail:]}"


def short_address(address: str) -> str:
    """checksummed address shortened for compact display: 0xA0b8…eB48."""
    return short_hex(to_checksum_address(address), head=4, tail=4)


def format_amount(value: int) -> str:
    """format a raw integer compactly; token decimals are unknown, so values stay unscaled."""
    if value == _MAX_UINT256:
        return "max"
    if value >= 1 << 64 and value & (value + 1) == 0:
        return f"uint{value.bit_length()} max"
    if abs(value) < 10**12:
        return f"{value:,}"
    return f"{value:.4g}".replace("e+", "e")


# --- event signatures --------------------------------------------------------

# declarations keep `indexed` and parameter names so topics and data can be labelled;
# topic0 is computed from the canonical signature, so a typo can never mislabel a log
KNOWN_EVENTS: tuple[str, ...] = (
    # ERC-20 and ERC-721 share topic0 for Transfer / Approval and differ in indexed params
    "Transfer(address indexed from, address indexed to, uint256 value)",
    "Transfer(address indexed from, address indexed to, uint256 indexed tokenId)",
    "Approval(address indexed owner, address indexed spender, uint256 value)",
    "Approval(address indexed owner, address indexed approved, uint256 indexed tokenId)",
    "ApprovalForAll(address indexed owner, address indexed operator, bool approved)",
    # ERC-1155
    "TransferSingle(address indexed operator, address indexed from, address indexed to, uint256 id, uint256 value)",
    "TransferBatch(address indexed operator, address indexed from, address indexed to, uint256[] ids, uint256[] values)",
    "URI(string value, uint256 indexed id)",
    # WETH
    "Deposit(address indexed dst, uint256 wad)",
    "Withdrawal(address indexed src, uint256 wad)",
    # ERC-4626 vaults
    "Deposit(address indexed sender, address indexed owner, uint256 assets, uint256 shares)",
    "Withdraw(address indexed sender, address indexed receiver, address indexed owner, uint256 assets, uint256 shares)",
    # Uniswap V2 pairs and factory (and most forks)
    "Swap(address indexed sender, uint256 amount0In, uint256 amount1In, uint256 amount0Out, uint256 amount1Out, address indexed to)",
    "Sync(uint112 reserve0, uint112 reserve1)",
    "Mint(address indexed sender, uint256 amount0, uint256 amount1)",
    "Burn(address indexed sender, uint256 amount0, uint256 amount1, address indexed to)",
    "PairCreated(address indexed token0, address indexed token1, address pair, uint256 index)",
    # Uniswap V3 pools, factory and position manager (and most forks)
    "Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick)",
    "Mint(address sender, address indexed owner, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount, uint256 amount0, uint256 amount1)",
    "Burn(address indexed owner, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount, uint256 amount0, uint256 amount1)",
    "Collect(address indexed owner, address recipient, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount0, uint128 amount1)",
    "Flash(address indexed sender, address indexed recipient, uint256 amount0, uint256 amount1, uint256 paid0, uint256 paid1)",
    "Initialize(uint160 sqrtPriceX96, int24 tick)",
    "PoolCreated(address indexed token0, address indexed token1, uint24 indexed fee, int24 tickSpacing, address pool)",
    "IncreaseLiquidity(uint256 indexed tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)",
    "DecreaseLiquidity(uint256 indexed tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)",
    "Collect(uint256 indexed tokenId, address recipient, uint256 amount0, uint256 amount1)",
    # Uniswap V4 pool manager
    "Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)",
    "ModifyLiquidity(bytes32 indexed id, address indexed sender, int24 tickLower, int24 tickUpper, int256 liquidityDelta, bytes32 salt)",
    # Permit2
    "Approval(address indexed owner, address indexed token, address indexed spender, uint160 amount, uint48 expiration)",
    "Permit(address indexed owner, address indexed token, address indexed spender, uint160 amount, uint48 expiration, uint48 nonce)",
    # Curve pools (stableswap and cryptoswap)
    "TokenExchange(address indexed buyer, int128 sold_id, uint256 tokens_sold, int128 bought_id, uint256 tokens_bought)",
    "TokenExchange(address indexed buyer, uint256 sold_id, uint256 tokens_sold, uint256 bought_id, uint256 tokens_bought)",
    # Balancer V2 vault
    "Swap(bytes32 indexed poolId, address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut)",
    # Aave V3 pool
    "Supply(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount, uint16 indexed referralCode)",
    "Withdraw(address indexed reserve, address indexed user, address indexed to, uint256 amount)",
    "Borrow(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)",
    "Repay(address indexed reserve, address indexed user, address indexed repayer, uint256 amount, bool useATokens)",
    "LiquidationCall(address indexed collateralAsset, address indexed debtAsset, address indexed user, uint256 debtToCover, uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)",
    # Chainlink aggregators
    "AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)",
    "NewRound(uint256 indexed roundId, address indexed startedBy, uint256 startedAt)",
    # OpenZeppelin ownership, proxies, access control and pausing
    "OwnershipTransferred(address indexed previousOwner, address indexed newOwner)",
    "Upgraded(address indexed implementation)",
    "AdminChanged(address previousAdmin, address newAdmin)",
    "RoleGranted(bytes32 indexed role, address indexed account, address indexed sender)",
    "RoleRevoked(bytes32 indexed role, address indexed account, address indexed sender)",
    "Paused(address account)",
    "Unpaused(address account)",
    "Initialized(uint8 version)",
    "Initialized(uint64 version)",
)

_STATIC_TYPE_RE = re.compile(r"address|bool|u?int\d{0,3}|bytes(?:[1-9]|[12]\d|3[0-2])")


@dataclass(frozen=True, slots=True)
class EventParam:
    type: str
    name: str
    indexed: bool


@dataclass(frozen=True, slots=True)
class EventSpec:
    name: str
    signature: str  # canonical form, e.g. Transfer(address,address,uint256)
    topic0: str
    params: tuple[EventParam, ...]
    indexed_count: int


@dataclass(frozen=True, slots=True)
class DecodedLog:
    name: str | None  # only set when topic0 is in the signature table
    args: tuple[tuple[str, str], ...]  # (label, formatted value) pairs


def _canonical_type(solidity_type: str) -> str:
    # the ABI spells out the uint / int aliases, e.g. uint[] -> uint256[]
    return re.sub(r"\b(u?int)\b", r"\g<1>256", solidity_type)


def parse_event(declaration: str) -> EventSpec:
    """parse a declaration like 'Transfer(address indexed from, address indexed to, uint256 value)'."""
    name, separator, rest = declaration.strip().partition("(")
    name = name.strip()
    if not separator or not rest.endswith(")") or not name.isidentifier():
        raise ValueError(f"invalid event declaration: {declaration!r}")
    params = []
    chunks = [chunk.strip() for chunk in rest[:-1].split(",") if chunk.strip()]
    for position, chunk in enumerate(chunks):
        type_, *modifiers = chunk.split()
        names = [token for token in modifiers if token != "indexed"]
        params.append(
            EventParam(
                type=_canonical_type(type_),
                name=names[0] if names else f"arg{position}",
                indexed="indexed" in modifiers,
            )
        )
    signature = f"{name}({','.join(param.type for param in params)})"
    return EventSpec(
        name=name,
        signature=signature,
        topic0="0x" + keccak256(signature.encode("ascii")).hex(),
        params=tuple(params),
        indexed_count=sum(param.indexed for param in params),
    )


def _build_index(declarations: Sequence[str]) -> dict[str, tuple[EventSpec, ...]]:
    index: dict[str, list[EventSpec]] = {}
    for declaration in declarations:
        spec = parse_event(declaration)
        index.setdefault(spec.topic0, []).append(spec)
    return {topic0: tuple(specs) for topic0, specs in index.items()}


_EVENTS_BY_TOPIC0 = _build_index(KNOWN_EVENTS)


def event_name(topic0: str) -> str | None:
    """look up the event name for a topic0 hash."""
    specs = _EVENTS_BY_TOPIC0.get(topic0.lower())
    return specs[0].name if specs else None


# --- log decoding --------------------------------------------------------------


def _format_word(solidity_type: str, word: str) -> str:
    """format one 32-byte ABI word (64 hex chars) holding a static value."""
    if solidity_type == "address":
        return short_address("0x" + word[-40:])
    if solidity_type == "bool":
        return "true" if int(word, 16) else "false"
    if solidity_type.startswith("bytes"):
        return short_hex("0x" + word[: 2 * int(solidity_type[5:])])
    value = int(word, 16)
    if solidity_type.startswith("int") and value >= 1 << 255:
        value -= 1 << 256  # ints are sign-extended to 256 bits
    return format_amount(value)


def _guess_word(word: str) -> str:
    """render an undeclared 32-byte word as an address, a small integer or shortened hex."""
    value = int(word or "0", 16)
    if word.startswith("0" * 24) and value >= 1 << 96:
        return short_address("0x" + word[-40:])
    if value < 1 << 64:
        return f"{value:,}"
    return short_hex("0x" + word)


def _hex_body(value: str) -> str:
    return (value[2:] if value[:2] in ("0x", "0X") else value).lower()


def decode_log(topics: Sequence[str], data: str) -> DecodedLog:
    """decode a log with the built-in signature table, falling back to heuristics."""
    if not topics:
        return DecodedLog(None, ())  # anonymous event: no topic0 to identify it
    words = [word.rjust(64, "0") for word in (_hex_body(topic) for topic in topics[1:])]
    data_hex = _hex_body(data)
    specs = _EVENTS_BY_TOPIC0.get(topics[0].lower(), ())
    spec = next((s for s in specs if s.indexed_count == len(words)), None)
    if spec is None:
        # unknown event, or a known name with a non-standard indexed layout
        args = [(f"topic{i}", _guess_word(word)) for i, word in enumerate(words, start=1)]
        if data_hex:
            args.append(("data", f"{len(data_hex) // 2}B"))
        return DecodedLog(specs[0].name if specs else None, tuple(args))

    topic_words = iter(words)
    data_words = [data_hex[i : i + 64] for i in range(0, len(data_hex) - 63, 64)]
    next_data = 0
    args = []
    for param in spec.params:
        static = _STATIC_TYPE_RE.fullmatch(param.type) is not None
        if param.indexed:
            word = next(topic_words)
            # indexed dynamic values (string, bytes, arrays) are stored as their keccak hash
            args.append((param.name, _format_word(param.type, word) if static else short_hex("0x" + word)))
        elif next_data < len(data_words):
            # dynamic values only leave an offset in the head, so they are elided
            value = _format_word(param.type, data_words[next_data]) if static else "…"
            args.append((param.name, value))
            next_data += 1
    return DecodedLog(spec.name, tuple(args))
