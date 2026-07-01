"""Safety-layer configuration for the `hl` execution CLI.

Hard-coded defaults live here. Callers override via environment variables
(prefixed `HL_`) — we intentionally don't read a TOML config yet to keep the
surface small. If we grow more knobs we can add a loader later.

Principles:
- Cash-secured DCA accumulation, not leveraged trading.
- Any override that loosens a guard must be explicit per-invocation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyConfig:
    # Hard upper bound on leverage the CLI will set. 1x = cash-secured.
    max_leverage: int = 1
    # Per-order notional cap in USD. Override via HL_MAX_NOTIONAL_USD.
    max_notional_usd: float = 200.0
    # Limit price must be within this many bps of current mid (10000 = 100%).
    slippage_bps: int = 100
    # Orders this large must be limit (not market) — prevents size+market slippage.
    require_limit_for_large_usd: float = 100.0
    # Funding-rate sanity: refuse longs if annualized funding exceeds this (%/yr).
    # Set to 0 to disable. Default 50% — rules out obvious crowded-long traps.
    max_funding_pct_annualized: float = 50.0
    # Default margin mode for set-leverage. "isolated" keeps positions siloed.
    default_margin_mode: str = "isolated"  # "isolated" | "cross"


def load() -> SafetyConfig:
    """Load safety config, allowing env-var overrides."""

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        return float(raw) if raw else default

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        return int(raw) if raw else default

    def _str(name: str, default: str) -> str:
        return os.environ.get(name, default)

    return SafetyConfig(
        max_leverage=_int("HL_MAX_LEVERAGE", 1),
        max_notional_usd=_float("HL_MAX_NOTIONAL_USD", 200.0),
        slippage_bps=_int("HL_SLIPPAGE_BPS", 100),
        require_limit_for_large_usd=_float("HL_REQUIRE_LIMIT_USD", 100.0),
        max_funding_pct_annualized=_float("HL_MAX_FUNDING_PCT", 50.0),
        default_margin_mode=_str("HL_MARGIN_MODE", "isolated"),
    )
