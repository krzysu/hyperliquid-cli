"""USD ↔ coin-unit conversion with Hyperliquid lot-size rounding.

HL's SDK takes order size in native coin units. Each asset has a `szDecimals`
field in the universe metadata that caps the decimal precision. Rounding
down to avoid min-size rejection on the server.
"""

from __future__ import annotations

import math

from hyperliquid.info import Info


def _split_pair(pair: str) -> tuple[str | None, str]:
    """Normalize a user-supplied pair to the HL coin name on the wire.

    `BTCUSD` / `ETH/USDC` → bare coin. `xyz:CL` is left intact (HIP-3 deployer
    prefix is part of the coin name).
    """
    if ":" in pair:
        dex = pair.split(":", 1)[0].lower()
        return dex, pair
    coin = pair.upper().replace("/", "")
    for suffix in ("USDC", "USDT", "USD"):
        if coin.endswith(suffix) and len(coin) > len(suffix):
            coin = coin[: -len(suffix)]
            break
    return None, coin


def dex_for(coin: str) -> str | None:
    """Extract HIP-3 deployer prefix from a resolved coin name (or None for native perps)."""
    return coin.split(":", 1)[0].lower() if ":" in coin else None


def get_sz_decimals(info: Info, coin: str) -> int:
    """Look up szDecimals for a coin. Supports HIP-3 dex-prefixed names."""
    dex = dex_for(coin)
    meta = info.meta() if dex is None else info.post("/info", {"type": "meta", "dex": dex})
    for a in meta["universe"]:
        if a["name"] == coin:
            return int(a["szDecimals"])
    raise ValueError(f"coin {coin!r} not in HL universe (dex={dex})")


def round_size(size: float, sz_decimals: int) -> float:
    """Round DOWN to the nearest valid lot to avoid min-size rejection."""
    factor = 10**sz_decimals
    return math.floor(size * factor) / factor


def usd_to_size(usd: float, mid: float, sz_decimals: int) -> float:
    """Convert a USD notional to coin size, rounded down to lot size."""
    if mid <= 0:
        raise ValueError(f"non-positive mid price: {mid}")
    return round_size(usd / mid, sz_decimals)


def size_to_usd(size: float, mid: float) -> float:
    return size * mid
