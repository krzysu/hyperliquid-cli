"""Preflight safety checks for Hyperliquid orders.

Every guard raises `PreflightError` — the CLI maps that to the
`preflight_error` envelope category and exits non-zero.

Guards (defaults in config.SafetyConfig):
  - max_leverage: hard upper bound (default 1x)
  - max_notional_usd: per-order cap (default $200)
  - slippage_bps: limit price must be within N bps of mid (default 100 = 1%)
  - require_limit_for_large_usd: market orders forbidden above this size
  - max_funding_pct_annualized: refuse longs when crowd-long drag is extreme
"""

from __future__ import annotations

from hyperliquid.info import Info

from .config import SafetyConfig
from .sizing import dex_for


class PreflightError(Exception):
    pass


def check_leverage(leverage: int, cfg: SafetyConfig) -> None:
    if leverage > cfg.max_leverage:
        raise PreflightError(
            f"leverage {leverage}x exceeds max_leverage {cfg.max_leverage}x "
            f"(set HL_MAX_LEVERAGE to override)"
        )
    if leverage < 1:
        raise PreflightError(f"leverage must be >= 1, got {leverage}")


def check_notional(size: float, price: float, cfg: SafetyConfig) -> None:
    notional = size * price
    if notional > cfg.max_notional_usd:
        raise PreflightError(
            f"notional ${notional:.2f} exceeds max_notional_usd ${cfg.max_notional_usd:.2f} "
            f"(set HL_MAX_NOTIONAL_USD to override)"
        )


def check_limit_for_large(size: float, price: float, tif: str, cfg: SafetyConfig) -> None:
    notional = size * price
    if notional > cfg.require_limit_for_large_usd and tif == "Ioc":
        raise PreflightError(
            f"market/IOC order for ${notional:.2f} exceeds require_limit_for_large_usd "
            f"${cfg.require_limit_for_large_usd:.2f} — use a limit (GTC) order instead"
        )


def check_slippage(limit_px: float, mid: float, is_buy: bool, cfg: SafetyConfig) -> None:
    """Limit price must be within `slippage_bps` of mid on the aggressive side.

    For a BUY we allow limit_px up to mid * (1 + bps); higher is overpaying.
    For a SELL we allow limit_px down to mid * (1 - bps); lower is underselling.
    Passive limits (below mid for buy, above mid for sell) are always OK.
    """
    if mid <= 0:
        raise PreflightError(f"non-positive mid price: {mid}")
    max_deviation = mid * cfg.slippage_bps / 10_000
    if is_buy and limit_px > mid + max_deviation:
        raise PreflightError(
            f"BUY limit_px {limit_px:.6g} > mid {mid:.6g} + {cfg.slippage_bps}bps "
            f"({mid + max_deviation:.6g}); refusing to overpay"
        )
    if not is_buy and limit_px < mid - max_deviation:
        raise PreflightError(
            f"SELL limit_px {limit_px:.6g} < mid {mid:.6g} - {cfg.slippage_bps}bps "
            f"({mid - max_deviation:.6g}); refusing to undersell"
        )


def check_funding(info: Info, coin: str, is_buy: bool, cfg: SafetyConfig) -> dict:
    """Block new longs when funding is extreme (crowded). Returns the funding
    context fetched (mark, oracle, funding, OI) so callers can use it.

    Only gates BUYs — SHORTs in high-funding conditions are contrarian-plausible.
    """
    dex = dex_for(coin)
    if dex is None:
        universe, ctxs = info.meta_and_asset_ctxs()
    else:
        r = info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex})
        universe, ctxs = r[0], r[1]

    ctx = None
    for asset, c in zip(universe["universe"], ctxs, strict=False):
        if asset["name"] == coin:
            ctx = c
            break
    if ctx is None:
        raise PreflightError(f"coin {coin!r} not found in HL universe")

    funding_hourly = float(ctx["funding"])
    funding_ann_pct = funding_hourly * 8760 * 100
    mark = float(ctx["markPx"])
    oracle = float(ctx["oraclePx"])

    out = {
        "mark_px": mark,
        "oracle_px": oracle,
        "funding_hourly": funding_hourly,
        "funding_ann_pct": funding_ann_pct,
        "open_interest": float(ctx["openInterest"]),
        "mid_px": float(ctx["midPx"]),
        "guard": "disabled" if cfg.max_funding_pct_annualized <= 0 else "active",
        "guard_threshold_pct": cfg.max_funding_pct_annualized,
    }

    if cfg.max_funding_pct_annualized <= 0:
        return out
    if is_buy and funding_ann_pct > cfg.max_funding_pct_annualized:
        raise PreflightError(
            f"{coin} funding at {funding_ann_pct:+.1f}%/yr exceeds max "
            f"{cfg.max_funding_pct_annualized:.1f}%/yr — crowded-long trap, "
            f"refusing entry (set HL_MAX_FUNDING_PCT=0 to disable)"
        )
    return out
