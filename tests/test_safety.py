"""Tests for hl/safety.py preflight guards."""

from __future__ import annotations

import pytest

from hl.config import SafetyConfig
from hl.safety import (
    PreflightError,
    check_leverage,
    check_limit_for_large,
    check_notional,
    check_slippage,
)


def _cfg(**overrides) -> SafetyConfig:
    defaults = {
        "max_leverage": 1,
        "max_notional_usd": 200.0,
        "slippage_bps": 100,
        "require_limit_for_large_usd": 100.0,
        "max_funding_pct_annualized": 50.0,
        "default_margin_mode": "isolated",
    }
    defaults.update(overrides)
    return SafetyConfig(**defaults)


class TestLeverageGuard:
    def test_1x_passes(self):
        check_leverage(1, _cfg())

    def test_exceeds_max_blocks(self):
        with pytest.raises(PreflightError, match="exceeds max_leverage"):
            check_leverage(2, _cfg(max_leverage=1))

    def test_zero_leverage_rejected(self):
        with pytest.raises(PreflightError, match="must be >= 1"):
            check_leverage(0, _cfg())


class TestNotionalGuard:
    def test_under_cap_passes(self):
        check_notional(0.001, 100_000, _cfg(max_notional_usd=200))

    def test_exceeds_cap_blocks(self):
        with pytest.raises(PreflightError, match="exceeds max_notional_usd"):
            check_notional(0.01, 100_000, _cfg(max_notional_usd=200))


class TestMarketSizeGuard:
    def test_small_market_ok(self):
        check_limit_for_large(0.001, 50_000, "Ioc", _cfg())  # $50 IOC is fine

    def test_large_market_blocked(self):
        with pytest.raises(PreflightError, match="exceeds require_limit_for_large_usd"):
            check_limit_for_large(0.01, 50_000, "Ioc", _cfg())  # $500 IOC blocked

    def test_large_limit_ok(self):
        # Same size but as a resting limit (GTC): fine — no slippage on size
        check_limit_for_large(0.01, 50_000, "Gtc", _cfg())


class TestSlippageGuard:
    def test_buy_below_mid_always_ok(self):
        """Passive buy (limit_px < mid) is never overpaying."""
        check_slippage(limit_px=100.0, mid=105.0, is_buy=True, cfg=_cfg(slippage_bps=100))

    def test_buy_above_mid_within_bps_ok(self):
        # 1% slippage tolerance → buy at mid+0.5% is fine
        check_slippage(limit_px=100.5, mid=100.0, is_buy=True, cfg=_cfg(slippage_bps=100))

    def test_buy_too_far_above_mid_blocked(self):
        # mid 100, slip 100bps = 1% → buy at 101.5 overpays
        with pytest.raises(PreflightError, match="refusing to overpay"):
            check_slippage(limit_px=101.5, mid=100.0, is_buy=True, cfg=_cfg(slippage_bps=100))

    def test_sell_above_mid_always_ok(self):
        """Passive sell (limit_px > mid) never underprices."""
        check_slippage(limit_px=110.0, mid=105.0, is_buy=False, cfg=_cfg(slippage_bps=100))

    def test_sell_below_mid_within_bps_ok(self):
        check_slippage(limit_px=99.5, mid=100.0, is_buy=False, cfg=_cfg(slippage_bps=100))

    def test_sell_too_far_below_mid_blocked(self):
        with pytest.raises(PreflightError, match="refusing to undersell"):
            check_slippage(limit_px=98.5, mid=100.0, is_buy=False, cfg=_cfg(slippage_bps=100))

    def test_non_positive_mid_errors(self):
        with pytest.raises(PreflightError, match="non-positive mid"):
            check_slippage(limit_px=100, mid=0, is_buy=True, cfg=_cfg())
