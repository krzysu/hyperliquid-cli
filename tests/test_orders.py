"""Tests for hl/orders.py — focuses on pure logic (market price computation,
response parsing) and argparse wiring. Does not touch the network.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from hl.orders import (
    _TIF_MAP,
    _build_order_type,
    _cleanup_orphan_legs,
    _ensure_leverage,
    _market_price,
    _new_cloid,
    _parse_order_response,
    _resolve_coin,
    _trigger_limit_price,
    _validate_cloid,
    cmd_bracket,
    cmd_close,
    cmd_order,
    cmd_set_leverage,
)


class TestResolveCoin:
    def test_strips_suffix(self):
        assert _resolve_coin("BTCUSD") == "BTC"

    def test_preserves_hip3(self):
        assert _resolve_coin("xyz:CL") == "xyz:CL"

    def test_bare(self):
        assert _resolve_coin("HYPE") == "HYPE"


class TestMarketPrice:
    def test_buy_aggressive(self):
        # mid 100, slip 100bps with 1bps safety shave → 99bps → 100.99
        assert _market_price(100.0, is_buy=True, slippage_bps=100) == pytest.approx(100.99)

    def test_sell_aggressive(self):
        assert _market_price(100.0, is_buy=False, slippage_bps=100) == pytest.approx(99.01)

    def test_buy_at_50bps(self):
        # 50bps - 1bps shave = 49bps → 1000 * 1.0049 = 1004.9
        assert abs(_market_price(1000.0, is_buy=True, slippage_bps=50) - 1004.9) < 1e-9

    def test_zero_slippage_does_not_underflow(self):
        # slippage_bps=0 (or 1) clamps to 0 — no negative slip
        assert _market_price(100.0, is_buy=True, slippage_bps=0) == 100.0
        assert _market_price(100.0, is_buy=True, slippage_bps=1) == 100.0

    def test_market_price_passes_check_slippage(self):
        # Regression: the very next preflight call must accept the price we
        # just computed. Boundary cases should never round outside the band.
        from hl.config import SafetyConfig
        from hl.safety import check_slippage

        cfg = SafetyConfig(slippage_bps=100)
        for mid in (0.0817, 100.0, 50_000.0, 0.000123):
            for is_buy in (True, False):
                px = _market_price(mid, is_buy=is_buy, slippage_bps=cfg.slippage_bps)
                check_slippage(px, mid, is_buy, cfg)  # must not raise


class TestTriggerLimitPrice:
    def test_sell_trigger_below(self):
        # sell-side trigger: limit on the aggressive side below the trigger
        # 45000 * (1 - 0.03) = 43650 with default 100bps slippage
        assert _trigger_limit_price(45000.0, is_buy=False, slippage_bps=100) == pytest.approx(43650.0)

    def test_buy_trigger_above(self):
        # buy-side trigger: limit on the aggressive side above the trigger
        assert _trigger_limit_price(50.0, is_buy=True, slippage_bps=100) == pytest.approx(51.5)

    def test_zero_slippage_uses_minimum_buffer(self):
        # Zero/1bps clamps to 1bps minimum buffer — no division by zero, no zero price
        px = _trigger_limit_price(100.0, is_buy=False, slippage_bps=0)
        assert px < 100.0
        assert px > 0.0


class TestCloid:
    def test_format(self):
        cloid = _new_cloid()
        assert cloid.startswith("0x")
        assert len(cloid) == 34  # 0x + 32 hex chars

    def test_unique(self):
        assert _new_cloid() != _new_cloid()


class TestParseOrderResponse:
    def test_resting(self):
        resp = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 12345}}]}},
        }
        out = _parse_order_response(resp, "0xabcd")
        assert out["status"] == "resting"
        assert out["oid"] == 12345
        assert out["cloid"] == "0xabcd"

    def test_filled(self):
        resp = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"oid": 9999, "totalSz": "0.01", "avgPx": "60000"}}]},
            },
        }
        out = _parse_order_response(resp, "0x0")
        assert out["status"] == "filled"
        assert out["oid"] == 9999
        assert out["filled"]["totalSz"] == "0.01"

    def test_error_in_statuses(self):
        resp = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"error": "insufficient margin"}]}},
        }
        out = _parse_order_response(resp, "0x0")
        assert out["status"] == "error"
        assert "insufficient margin" in out["error"]

    def test_top_level_error(self):
        resp = {"status": "err", "response": "rate limited"}
        out = _parse_order_response(resp, "0x0")
        assert out["status"] == "error"
        assert "rate limited" in out["error"]

    def test_malformed_returns_unknown(self):
        out = _parse_order_response({"random": "shape"}, "0xabc")
        assert out["status"] == "unknown"
        assert out["oid"] is None


def _args(**kw):
    ns = argparse.Namespace(
        type="limit", post_only=False, ioc=False, fok=False,
        trigger_px=None, tpsl=None,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestBuildOrderType:
    def test_default_is_limit_gtc(self):
        ot, tif = _build_order_type(_args())
        assert ot == {"limit": {"tif": "Gtc"}}
        assert tif == "Gtc"

    def test_market_is_ioc(self):
        ot, tif = _build_order_type(_args(type="market"))
        assert ot == {"limit": {"tif": "Ioc"}}
        assert tif == "Ioc"

    def test_post_only_is_alo(self):
        ot, tif = _build_order_type(_args(post_only=True))
        assert ot == {"limit": {"tif": "Alo"}}
        assert tif == "Alo"

    def test_ioc_flag(self):
        ot, tif = _build_order_type(_args(ioc=True))
        assert ot == {"limit": {"tif": "Ioc"}}
        assert tif == "Ioc"

    def test_trigger_stop(self):
        ot, tif = _build_order_type(_args(trigger_px=50000, tpsl="sl"))
        assert ot == {"trigger": {"triggerPx": 50000.0, "isMarket": True, "tpsl": "sl"}}
        assert tif == "trigger-sl"

    def test_trigger_tp(self):
        ot, tif = _build_order_type(_args(trigger_px=85000, tpsl="tp"))
        assert ot == {"trigger": {"triggerPx": 85000.0, "isMarket": True, "tpsl": "tp"}}
        assert tif == "trigger-tp"


class TestModifyTifMap:
    def test_all_choices_present(self):
        assert _TIF_MAP == {"gtc": "Gtc", "alo": "Alo", "ioc": "Ioc"}


class TestCleanupOrphanLegs:
    def test_returns_none_if_entry_filled(self):
        legs = [
            {"leg": "entry", "status": "filled", "oid": 1},
            {"leg": "stop", "status": "resting", "oid": 2},
            {"leg": "tp", "status": "resting", "oid": 3},
        ]
        assert _cleanup_orphan_legs(MagicMock(), "BTC", legs) is None

    def test_returns_none_if_entry_resting(self):
        # Limit entry that's resting is fine — stop/tp will fire after fill.
        legs = [
            {"leg": "entry", "status": "resting", "oid": 1},
            {"leg": "stop", "status": "resting", "oid": 2},
            {"leg": "tp", "status": "resting", "oid": 3},
        ]
        assert _cleanup_orphan_legs(MagicMock(), "BTC", legs) is None

    def test_cancels_resting_exit_legs_when_entry_errors(self):
        legs = [
            {"leg": "entry", "status": "error", "error": "insufficient margin"},
            {"leg": "stop", "status": "resting", "oid": 2},
            {"leg": "tp", "status": "resting", "oid": 3},
        ]
        exchange = MagicMock()
        exchange.bulk_cancel.return_value = {"status": "ok"}
        result = _cleanup_orphan_legs(exchange, "BTC", legs)
        exchange.bulk_cancel.assert_called_once_with([
            {"coin": "BTC", "oid": 2},
            {"coin": "BTC", "oid": 3},
        ])
        assert result["cancelled"] == 2
        assert result["oids"] == [2, 3]

    def test_no_cancel_when_entry_errors_but_exits_also_errored(self):
        legs = [
            {"leg": "entry", "status": "error", "error": "x"},
            {"leg": "stop", "status": "error", "error": "y"},
            {"leg": "tp", "status": "error", "error": "z"},
        ]
        exchange = MagicMock()
        result = _cleanup_orphan_legs(exchange, "BTC", legs)
        exchange.bulk_cancel.assert_not_called()
        assert "no resting exit legs" in result["reason"]

    def test_swallows_cancel_failure(self):
        legs = [
            {"leg": "entry", "status": "error", "error": "x"},
            {"leg": "stop", "status": "resting", "oid": 2},
        ]
        exchange = MagicMock()
        exchange.bulk_cancel.side_effect = RuntimeError("network down")
        result = _cleanup_orphan_legs(exchange, "BTC", legs)
        assert result["cancelled"] == 0
        assert "network down" in result["error"]
        assert result["attempted"] == [2]


def _envelope_from_capsys(capsys) -> dict:
    """Pull the JSON envelope `fail()` printed to stdout."""
    out = capsys.readouterr().out
    return json.loads(out.strip().splitlines()[-1])


class TestValidateCloid:
    def test_valid(self):
        cloid = "0x" + "a" * 32
        assert _validate_cloid(cloid) == cloid

    def test_too_short(self, capsys):
        with pytest.raises(SystemExit):
            _validate_cloid("0xabcd")
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_no_prefix(self, capsys):
        with pytest.raises(SystemExit):
            _validate_cloid("a" * 32)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_non_hex(self, capsys):
        with pytest.raises(SystemExit):
            _validate_cloid("0x" + "z" * 32)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"


class TestBuildOrderTypeRejections:
    def test_market_with_post_only_rejected(self, capsys):
        with pytest.raises(SystemExit):
            _build_order_type(_args(type="market", post_only=True))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"
        assert "market" in env["message"].lower()

    def test_market_with_ioc_rejected(self, capsys):
        with pytest.raises(SystemExit):
            _build_order_type(_args(type="market", ioc=True))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_market_with_fok_rejected(self, capsys):
        with pytest.raises(SystemExit):
            _build_order_type(_args(type="market", fok=True))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_post_only_and_ioc_both_set_rejected(self, capsys):
        with pytest.raises(SystemExit):
            _build_order_type(_args(post_only=True, ioc=True))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"


class TestEnsureLeverage:
    def test_success_logs_and_returns(self):
        exchange = MagicMock()
        exchange.update_leverage.return_value = {"status": "ok"}
        _ensure_leverage(exchange, "BTC", 1, is_cross=False)  # must not raise
        exchange.update_leverage.assert_called_once_with(1, "BTC", False)

    def test_failure_aborts_with_execution_error(self, capsys):
        exchange = MagicMock()
        exchange.update_leverage.side_effect = RuntimeError("network down")
        with pytest.raises(SystemExit):
            _ensure_leverage(exchange, "BTC", 2, is_cross=False)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "execution_error"
        assert "leverage update failed" in env["message"]

    def test_err_response_aborts_with_execution_error(self, capsys):
        # HL returns {"status": "err", "response": "..."} for cases like
        # reducing leverage with an open position. Must be treated as a
        # hard failure, not a success.
        exchange = MagicMock()
        exchange.update_leverage.return_value = {
            "status": "err",
            "response": "cannot reduce leverage with open position",
        }
        with pytest.raises(SystemExit):
            _ensure_leverage(exchange, "BTC", 1, is_cross=False)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "execution_error"
        assert "rejected by exchange" in env["message"]
        assert "cannot reduce leverage" in env["message"]


def _bracket_args(**kw):
    """Build a Namespace shaped like the bracket subparser."""
    ns = argparse.Namespace(
        side="buy",
        coin="BTC",
        size=0.001,
        usd=None,
        entry_type="limit",
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        leverage=1,
        preview=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestBracketShape:
    """End-to-end shape test for cmd_bracket — ensures the three legs are
    constructed with the correct sides, reduce_only flags, and trigger
    payloads. Stops or TPs flipping sides would be a serious bug."""

    def _run_bracket(self, args, mock_response):
        # Patch every external touchpoint
        with patch("hl.orders.make_info") as p_info, \
             patch("hl.orders.make_exchange") as p_exch, \
             patch("hl.orders._resolve_size", return_value=(0.001, 100.0)), \
             patch("hl.orders.check_funding", return_value={"guard": "active"}):
            info = MagicMock()
            p_info.return_value = info
            exch = MagicMock()
            exch.bulk_orders.return_value = mock_response
            p_exch.return_value = (exch, "0xabc")
            cmd_bracket(args)
            return exch

    def test_buy_bracket_shape(self):
        mock_response = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"resting": {"oid": 100}},
                {"resting": {"oid": 200}},
                {"resting": {"oid": 300}},
            ]}},
        }
        exch = self._run_bracket(_bracket_args(), mock_response)

        # update_leverage called before bulk_orders
        exch.update_leverage.assert_called_once()
        exch.bulk_orders.assert_called_once()
        legs, kwargs = exch.bulk_orders.call_args.args[0], exch.bulk_orders.call_args.kwargs
        assert kwargs.get("grouping") == "positionTpsl"
        assert len(legs) == 3

        entry, stop, tp = legs
        # Entry: buy=True, reduce_only=False
        assert entry["is_buy"] is True
        assert entry["reduce_only"] is False
        assert entry["limit_px"] == 100.0
        # Stop: opposite side, reduce_only=True, trigger 'sl'
        assert stop["is_buy"] is False
        assert stop["reduce_only"] is True
        assert stop["order_type"]["trigger"]["tpsl"] == "sl"
        assert stop["order_type"]["trigger"]["triggerPx"] == 90.0
        # TP: opposite side, reduce_only=True, trigger 'tp'
        assert tp["is_buy"] is False
        assert tp["reduce_only"] is True
        assert tp["order_type"]["trigger"]["tpsl"] == "tp"
        assert tp["order_type"]["trigger"]["triggerPx"] == 120.0

    def test_sell_bracket_shape(self):
        mock_response = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"resting": {"oid": 1}}, {"resting": {"oid": 2}}, {"resting": {"oid": 3}},
            ]}},
        }
        # SELL needs tp < entry < stop
        args = _bracket_args(side="sell", entry_price=100.0, stop_loss=110.0, take_profit=80.0)
        exch = self._run_bracket(args, mock_response)
        legs = exch.bulk_orders.call_args.args[0]
        entry, stop, tp = legs
        assert entry["is_buy"] is False
        # Exits flip to buy
        assert stop["is_buy"] is True and stop["reduce_only"] is True
        assert tp["is_buy"] is True and tp["reduce_only"] is True
        assert stop["order_type"]["trigger"]["triggerPx"] == 110.0
        assert tp["order_type"]["trigger"]["triggerPx"] == 80.0

    def test_buy_invalid_stop_above_entry_rejected(self, capsys):
        args = _bracket_args(stop_loss=150.0)  # stop > entry → invalid for BUY
        with pytest.raises(SystemExit):
            self._run_bracket(args, {})
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"
        assert "BUY" in env["message"]

    def test_leverage_failure_aborts_before_bulk_orders(self, capsys):
        with patch("hl.orders.make_info") as p_info, \
             patch("hl.orders.make_exchange") as p_exch, \
             patch("hl.orders._resolve_size", return_value=(0.001, 100.0)), \
             patch("hl.orders.check_funding", return_value={"guard": "active"}):
            p_info.return_value = MagicMock()
            exch = MagicMock()
            exch.update_leverage.side_effect = RuntimeError("auth bad")
            p_exch.return_value = (exch, "0xabc")
            with pytest.raises(SystemExit):
                cmd_bracket(_bracket_args())
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "execution_error"
        assert "leverage update failed" in env["message"]
        # bulk_orders never called
        exch.bulk_orders.assert_not_called()

    def test_large_market_entry_blocked_by_check_limit_for_large(self, capsys):
        # size=0.003 BTC, mid=50_000 → ~$151 notional
        # That's under max_notional_usd=$200 but over require_limit_for_large_usd=$100,
        # so the limit-for-large guard must fire on a market entry.
        # BUY geometry: stop < entry < tp; entry will be ~$50,495 (aggressive IOC at mid).
        args = _bracket_args(
            entry_type="market",
            entry_price=None,
            size=0.003,
            stop_loss=49_000.0,
            take_profit=60_000.0,
        )
        with patch("hl.orders.make_info"), \
             patch("hl.orders._resolve_size", return_value=(0.003, 50_000.0)), \
             patch("hl.orders.check_funding", return_value=None), pytest.raises(SystemExit):
            cmd_bracket(args)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "preflight_error"
        assert "require_limit_for_large" in env["message"]


def _order_args(**kw):
    """Build a Namespace shaped like the order subparser."""
    ns = argparse.Namespace(
        side="buy",
        coin="BTC",
        size=0.001,
        usd=None,
        type="limit",
        price=None,
        leverage=1,
        reduce_only=False,
        post_only=False,
        ioc=False,
        fok=False,
        trigger_px=None,
        tpsl=None,
        client_order_id=None,
        preview=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestCmdOrderTrigger:
    """The README example `hl order sell BTC 0.01 --trigger-px 45000 --tpsl sl --reduce-only`
    must work without forcing --price or --type market. limit_px is derived
    from the trigger with a slippage buffer."""

    def _run_order(self, args, size=0.004, mid=100_000.0, mock_response=None):
        with patch("hl.orders.make_info"), \
             patch("hl.orders._resolve_size", return_value=(size, mid)), \
             patch("hl.orders.check_funding", return_value=None):
            exch = MagicMock()
            exch.update_leverage.return_value = {"status": "ok"}
            exch.order.return_value = mock_response or {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
            }
            with patch("hl.orders.make_exchange", return_value=(exch, "0xabc")):
                cmd_order(args)
                return exch

    def test_trigger_without_price_derives_limit_px(self):
        # SELL trigger at 45000 with default 100bps → limit = 45000 * (1 - 0.03) = 43650
        # size=0.004 * 43650 = $174.60 < $200 notional cap
        args = _order_args(side="sell", trigger_px=45000.0, tpsl="sl", reduce_only=True)
        exch = self._run_order(args, size=0.004, mid=100_000.0)
        order_call = exch.order.call_args
        assert order_call.kwargs["limit_px"] == pytest.approx(43650.0)
        assert order_call.kwargs["order_type"]["trigger"]["triggerPx"] == 45000.0
        assert order_call.kwargs["order_type"]["trigger"]["tpsl"] == "sl"

    def test_explicit_price_wins_over_trigger(self):
        # If user passes both --price and --trigger-px, --price wins.
        args = _order_args(side="sell", trigger_px=45000.0, tpsl="sl", price=44000.0, reduce_only=True)
        exch = self._run_order(args, size=0.004, mid=100_000.0)
        assert exch.order.call_args.kwargs["limit_px"] == 44000.0

    def test_buy_trigger_limit_above(self):
        # BUY trigger: limit past trigger on the aggressive (above) side
        args = _order_args(side="buy", trigger_px=100.0, tpsl="tp")
        exch = self._run_order(args, size=0.001, mid=100.0)
        # 100 * (1 + 0.03) = 103
        assert exch.order.call_args.kwargs["limit_px"] == pytest.approx(103.0)

    def test_no_trigger_no_price_no_type_market_errors(self, capsys):
        args = _order_args(price=None)
        with patch("hl.orders.make_info"), \
             patch("hl.orders._resolve_size", return_value=(0.01, 100.0)), \
             patch("hl.orders.check_funding", return_value=None), pytest.raises(SystemExit):
            cmd_order(args)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"
        assert "--price is required" in env["message"]


class TestCmdSetLeverageErrResponse:
    def test_err_response_aborts_with_execution_error(self, capsys):
        from hl.config import SafetyConfig

        ns = argparse.Namespace(coin="BTC", leverage=1, isolated=False, cross=False)
        with patch("hl.orders.hl_config.load", return_value=SafetyConfig()), \
             patch("hl.orders.make_exchange") as p_exch:
            exch = MagicMock()
            exch.update_leverage.return_value = {"status": "err", "response": "leverage would cross"}
            p_exch.return_value = (exch, "0xabc")
            with pytest.raises(SystemExit):
                cmd_set_leverage(ns)
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "execution_error"
        assert "rejected by exchange" in env["message"]

    def test_ok_response_emits_ok_true(self, capsys):
        from hl.config import SafetyConfig

        ns = argparse.Namespace(coin="BTC", leverage=1, isolated=False, cross=False)
        with patch("hl.orders.hl_config.load", return_value=SafetyConfig()), \
             patch("hl.orders.make_exchange") as p_exch:
            exch = MagicMock()
            exch.update_leverage.return_value = {"status": "ok"}
            p_exch.return_value = (exch, "0xabc")
            cmd_set_leverage(ns)
        env = _envelope_from_capsys(capsys)
        assert env.get("ok") is True


def _close_args(**kw):
    """Build a Namespace shaped like the close subparser."""
    ns = argparse.Namespace(coin="BTC", size_pct=100.0, preview=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestCmdCloseSizePctBounds:
    def _run_close(self, args, mock_response=None):
        from hl.config import SafetyConfig
        with patch("hl.orders.hl_config.load", return_value=SafetyConfig()), \
             patch("hl.orders.make_info") as p_info, \
             patch("hl.orders.make_exchange") as p_exch:
            info = MagicMock()
            info.user_state.return_value = {
                "assetPositions": [{"position": {"coin": "BTC", "szi": "0.5"}}]
            }
            info.all_mids.return_value = {"BTC": "50000.0"}
            info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 3}]}
            p_info.return_value = info
            exch = MagicMock()
            exch.order.return_value = mock_response or {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
            }
            p_exch.return_value = (exch, "0xabc")
            cmd_close(args)
            return exch

    def test_size_pct_over_100_rejected(self, capsys):
        with patch("hl.orders.hl_config.load"), \
             patch("hl.orders.make_info"), \
             patch("hl.orders.make_exchange", return_value=(MagicMock(), "0xabc")), pytest.raises(SystemExit):
            cmd_close(_close_args(size_pct=200.0))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"
        assert "size-pct" in env["message"]

    def test_size_pct_zero_rejected(self, capsys):
        with patch("hl.orders.hl_config.load"), \
             patch("hl.orders.make_info"), \
             patch("hl.orders.make_exchange", return_value=(MagicMock(), "0xabc")), pytest.raises(SystemExit):
            cmd_close(_close_args(size_pct=0.0))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_size_pct_negative_rejected(self, capsys):
        with patch("hl.orders.hl_config.load"), \
             patch("hl.orders.make_info"), \
             patch("hl.orders.make_exchange", return_value=(MagicMock(), "0xabc")), pytest.raises(SystemExit):
            cmd_close(_close_args(size_pct=-5.0))
        env = _envelope_from_capsys(capsys)
        assert env["error"] == "validation"

    def test_size_pct_100_works(self):
        exch = self._run_close(_close_args(size_pct=100.0))
        # 0.5 BTC * 1.0 → rounded, IOC submitted
        exch.order.assert_called_once()
