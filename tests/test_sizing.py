"""Tests for hl/sizing.py — USD↔coin conversion + lot rounding."""

from __future__ import annotations

import pytest

from hl.sizing import _split_pair, dex_for, round_size, size_to_usd, usd_to_size


class TestSplitPair:
    def test_native_usd(self):
        assert _split_pair("BTCUSD") == (None, "BTC")

    def test_native_usdc(self):
        assert _split_pair("ETHUSDC") == (None, "ETH")

    def test_native_usdt(self):
        assert _split_pair("SOLUSDT") == (None, "SOL")

    def test_slash(self):
        assert _split_pair("ETH/USD") == (None, "ETH")

    def test_bare_coin(self):
        assert _split_pair("HYPE") == (None, "HYPE")

    def test_hip3_xyz(self):
        assert _split_pair("xyz:CL") == ("xyz", "xyz:CL")

    def test_hip3_case_normalized(self):
        assert _split_pair("XYZ:CL") == ("xyz", "XYZ:CL")


class TestRoundSize:
    def test_rounds_down(self):
        # 0.12345 at 4 decimals → 0.1234 (down, never up)
        assert round_size(0.12345, 4) == 0.1234

    def test_no_rounding_needed(self):
        assert round_size(0.1, 4) == 0.1

    def test_zero_decimals(self):
        assert round_size(1.9, 0) == 1.0

    def test_more_precision_than_needed(self):
        assert round_size(0.123456789, 5) == 0.12345


class TestUsdToSize:
    def test_basic(self):
        # $500 at $50k mid, 4 decimals → 0.01 BTC
        assert usd_to_size(500, 50_000, 4) == 0.01

    def test_rounding_down(self):
        # $100 at $77825 mid = 0.001285... BTC; at 4 decimals → 0.0012
        assert usd_to_size(100, 77_825, 4) == 0.0012

    def test_zero_mid_errors(self):
        with pytest.raises(ValueError, match="non-positive mid"):
            usd_to_size(100, 0, 4)

    def test_negative_mid_errors(self):
        with pytest.raises(ValueError, match="non-positive mid"):
            usd_to_size(100, -1, 4)


class TestSizeToUsd:
    def test_basic(self):
        assert size_to_usd(0.5, 100) == 50.0


class TestDexFor:
    def test_native_returns_none(self):
        assert dex_for("BTC") is None

    def test_hip3_returns_dex(self):
        assert dex_for("xyz:CL") == "xyz"

    def test_hip3_lowercased(self):
        assert dex_for("XYZ:CL") == "xyz"
