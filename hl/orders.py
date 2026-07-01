"""Order placement, cancel, modify, leverage, and close commands.

Every command goes through:
  1. Auth load (API wallet; main-key guard at startup)
  2. Preflight safety checks (leverage / notional / slippage / funding)
  3. Execution (SDK call) — unless --preview, which prints the payload
  4. Structured JSON response via envelope.emit
  5. Attempt logged to logs/hl-execution/YYYY-MM-DD.jsonl

Size handling:
  - Positional SIZE is in coin units (e.g. 0.01 BTC)
  - --usd N computes SIZE from current mid, rounded down to szDecimals

Order styles:
  - --type limit --price P  (default)  → GTC by default, --post-only → Alo, --ioc → Ioc
  - --type market           → IOC at mid-crossing price (buy: mid * (1 + slip); sell: mid * (1 - slip))
  - --reduce-only           → flag; for closing positions without opening new ones
"""

from __future__ import annotations

import argparse
import re
import uuid

from hyperliquid.info import Info
from hyperliquid.utils.signing import Cloid, OrderType

from . import config as hl_config
from .auth import make_exchange, make_info
from .envelope import emit, fail
from .order_logger import Timer, log_attempt
from .safety import PreflightError, check_funding, check_leverage, check_limit_for_large, check_notional, check_slippage
from .sizing import _split_pair, dex_for, get_sz_decimals, round_size, usd_to_size

# A valid HL cloid is a 0x-prefixed 16-byte hex string (32 hex chars).
_CLOID_RE = re.compile(r"^0x[0-9a-fA-F]{32}$")


def _resolve_coin(coin_arg: str) -> str:
    """Allow the user to pass 'BTCUSD' or 'BTC' or 'xyz:CL' — normalize to HL coin name."""
    _, coin = _split_pair(coin_arg)
    return coin


def _resolve_size(info: Info, coin: str, size_native: float | None, usd: float | None) -> tuple[float, float]:
    """Return (size_in_coin_units, mid_price_used). Exactly one of size_native/usd is set."""
    if (size_native is None) == (usd is None):
        fail("validation", "provide either SIZE (positional, in coin units) OR --usd N, not both")

    sz_dec = get_sz_decimals(info, coin)
    dex = dex_for(coin)
    mids = info.all_mids() if dex is None else info.post("/info", {"type": "allMids", "dex": dex})
    if coin not in mids:
        fail("validation", f"no mid price for {coin!r}")
    mid = float(mids[coin])

    if size_native is not None:
        size = round_size(size_native, sz_dec)
        if size <= 0:
            fail("validation", f"size rounded to zero at szDecimals={sz_dec}")
        return size, mid
    # --usd path
    size = usd_to_size(usd, mid, sz_dec)
    if size <= 0:
        fail("validation", f"${usd:.2f} rounds to zero coins at mid {mid:.6g} (szDecimals={sz_dec})")
    return size, mid


def _build_order_type(args: argparse.Namespace) -> tuple[OrderType, str]:
    """Return (OrderType dict, tif-or-tag string) for the SDK call.

    Trigger orders (stops/take-profits) short-circuit this — they're
    `{"trigger": {"triggerPx": P, "isMarket": True, "tpsl": "sl"|"tp"}}`
    and conventionally pair with --reduce-only.
    """
    # Trigger order path
    if getattr(args, "trigger_px", None) is not None:
        if args.tpsl not in ("tp", "sl"):
            fail("validation", "--trigger-px requires --tpsl tp or --tpsl sl")
        return (
            {"trigger": {"triggerPx": float(args.trigger_px), "isMarket": True, "tpsl": args.tpsl}},
            f"trigger-{args.tpsl}",
        )

    if args.type == "market":
        # Market orders are always IOC at an aggressive price; explicit TIF
        # flags would be silently dropped, so reject up front.
        if args.post_only or args.ioc or args.fok:
            fail("validation", "--post-only / --ioc / --fok cannot combine with --type market (market is IOC)")
        return {"limit": {"tif": "Ioc"}}, "Ioc"
    # limit
    if args.post_only and args.ioc:
        fail("validation", "--post-only and --ioc are mutually exclusive")
    if args.post_only:
        return {"limit": {"tif": "Alo"}}, "Alo"
    if args.ioc:
        return {"limit": {"tif": "Ioc"}}, "Ioc"
    if args.fok:
        # HL has no true FOK; closest is IOC with a strict price. Reject explicitly.
        fail("validation", "Hyperliquid does not support FOK; use --post-only or --ioc")
    return {"limit": {"tif": "Gtc"}}, "Gtc"


def _market_price(mid: float, is_buy: bool, slippage_bps: int) -> float:
    """Compute an aggressive limit price that guarantees a market fill via IOC.

    Stays one bps inside `check_slippage`'s allowed band so the very-next
    preflight call never rejects this price due to floating-point rounding
    at the boundary (e.g. `mid * (1 + bps) > mid + mid*bps` can be true).
    """
    safe_bps = max(0, slippage_bps - 1)
    slip = safe_bps / 10_000
    return mid * (1 + slip) if is_buy else mid * (1 - slip)


def _trigger_limit_price(trigger_px: float, is_buy: bool, slippage_bps: int) -> float:
    """Compute limit_px for an `isMarket=True` trigger order.

    When HL fires a market trigger (stop / TP), it sends an IOC at
    `limit_px`. HL treats `limit_px` as a slippage cap — if the market has
    moved past it, the IOC fails rather than fills at a worse price. We
    place the limit past the trigger on the aggressive side with a 3x
    slippage buffer so a normal fill is almost always possible, but a
    runaway move still aborts the close instead of executing far from
    intent.
    """
    buffer_bps = max(1, slippage_bps * 3)
    buf = buffer_bps / 10_000
    return trigger_px * (1 + buf) if is_buy else trigger_px * (1 - buf)


def _new_cloid() -> str:
    """Generate a client order id. HL accepts hex-string format."""
    return "0x" + uuid.uuid4().hex


def _validate_cloid(cloid: str) -> str:
    """Reject malformed cloids early so the user sees a `validation` envelope
    rather than a raw SDK exception bubbling up as `execution_error`."""
    if not _CLOID_RE.match(cloid):
        fail("validation", f"--client-order-id must match 0x + 32 hex chars (got {cloid!r}, len={len(cloid)})")
    return cloid


def _ensure_leverage(exchange, coin: str, leverage: int, is_cross: bool) -> None:
    """Set leverage and abort the order if it fails for any reason.

    HL accepts repeated set-leverage calls idempotently, so a failure here
    is a real failure (auth, network, rate-limit, or invalid params) — not
    a no-op. Proceeding could place the order at the wrong leverage and
    silently violate the user's intent or notional caps.

    `update_leverage` can return `{"status": "err", "response": "..."}`
    without raising (e.g. open position in the opposite direction, or
    attempting to reduce leverage with a position open). Treated as a hard
    failure — same downstream impact as a raised exception.
    """
    request = {"coin": coin, "leverage": leverage, "is_cross": is_cross}
    with Timer() as t:
        try:
            response = exchange.update_leverage(leverage, coin, is_cross)
        except Exception as exc:  # noqa: BLE001
            log_attempt("set-leverage", request, None, t.elapsed, str(exc))
            fail(
                "execution_error",
                f"leverage update failed before order placement ({type(exc).__name__}: {exc}); "
                f"refusing to place order at unknown leverage",
            )

        if isinstance(response, dict) and response.get("status") == "err":
            msg = response.get("response") or str(response)
            log_attempt("set-leverage", request, response, t.elapsed, msg)
            fail(
                "execution_error",
                f"leverage update rejected by exchange: {msg}; "
                f"refusing to place order at unknown leverage",
            )
    log_attempt("set-leverage", request, response, t.elapsed_ms)


def cmd_order(args: argparse.Namespace) -> None:
    cfg = hl_config.load()
    info = make_info()
    coin = _resolve_coin(args.coin)
    is_buy = args.side == "buy"

    size, mid = _resolve_size(info, coin, args.size, args.usd)

    # Determine limit price. Priority:
    #   1. --price (explicit override)
    #   2. --trigger-px (derive a slippage-capped limit past the trigger)
    #   3. --type market (aggressive IOC at mid)
    #   4. --type limit (default — requires --price)
    if args.price is not None:
        limit_px = float(args.price)
    elif getattr(args, "trigger_px", None) is not None:
        limit_px = _trigger_limit_price(args.trigger_px, is_buy, cfg.slippage_bps)
    elif args.type == "market":
        limit_px = _market_price(mid, is_buy, cfg.slippage_bps)
    else:
        fail("validation", "--price is required for --type limit (the default)")

    order_type, tif = _build_order_type(args)

    # Preflight — funding guard only on buys (entry protection), skipped on --reduce-only
    try:
        check_leverage(args.leverage, cfg)
        check_notional(size, limit_px, cfg)
        check_limit_for_large(size, limit_px, tif, cfg)
        # For trigger orders, limit_px is a slippage cap on the triggered IOC,
        # not a price in the current market — skip the slippage check.
        if getattr(args, "trigger_px", None) is None:
            check_slippage(limit_px, mid, is_buy, cfg)
        fund_ctx = None
        if is_buy and not args.reduce_only:
            fund_ctx = check_funding(info, coin, is_buy, cfg)
    except PreflightError as exc:
        fail("preflight_error", str(exc))

    cloid = _validate_cloid(args.client_order_id) if args.client_order_id else _new_cloid()
    request = {
        "action": "order",
        "coin": coin,
        "is_buy": is_buy,
        "sz": size,
        "limit_px": limit_px,
        "order_type": order_type,
        "reduce_only": args.reduce_only,
        "cloid": cloid,
        "mid_at_submit": mid,
        "notional_usd": round(size * limit_px, 4),
        "leverage": args.leverage,
    }

    if args.preview:
        emit({"ok": True, "preview": True, "request": request, "funding_ctx": fund_ctx})
        return

    exchange, _ = make_exchange()

    # Set leverage explicitly so cross/isolated state can't drift between runs.
    # Aborts on failure — see _ensure_leverage docstring.
    is_cross = cfg.default_margin_mode == "cross"
    _ensure_leverage(exchange, coin, args.leverage, is_cross)

    with Timer() as t:
        try:
            response = exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_px,
                order_type=order_type,
                reduce_only=args.reduce_only,
                cloid=Cloid.from_str(cloid),
            )
        except Exception as exc:  # noqa: BLE001
            log_attempt("order", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")

    log_attempt("order", request, response, t.elapsed_ms)
    parsed = _parse_order_response(response, cloid)
    emit({"ok": True, "request": request, "response": response, "parsed": parsed, "latency_ms": round(t.elapsed_ms, 2)})


def _parse_order_response(response: dict, cloid: str) -> dict:
    """Extract (status, oid) from HL's nested response shape."""
    out = {"cloid": cloid, "status": "unknown", "oid": None, "filled": None, "error": None}
    if not isinstance(response, dict):
        return out
    if response.get("status") == "err":
        out["status"] = "error"
        out["error"] = response.get("response") or str(response)
        return out
    try:
        statuses = response["response"]["data"]["statuses"]
        if not statuses:
            return out
        s = statuses[0]
        if "resting" in s:
            out["status"] = "resting"
            out["oid"] = s["resting"].get("oid")
        elif "filled" in s:
            out["status"] = "filled"
            out["oid"] = s["filled"].get("oid")
            out["filled"] = s["filled"]
        elif "error" in s:
            out["status"] = "error"
            out["error"] = s["error"]
    except (KeyError, TypeError, IndexError):
        pass
    return out


def cmd_cancel(args: argparse.Namespace) -> None:
    coin = _resolve_coin(args.coin)
    exchange, _ = make_exchange()
    request = {"action": "cancel", "coin": coin, "oid": args.order_id}
    with Timer() as t:
        try:
            response = exchange.cancel(coin, int(args.order_id))
        except Exception as exc:  # noqa: BLE001
            log_attempt("cancel", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")
    log_attempt("cancel", request, response, t.elapsed_ms)
    emit({"ok": True, "request": request, "response": response, "latency_ms": round(t.elapsed_ms, 2)})


def cmd_cancel_all(args: argparse.Namespace) -> None:
    info = make_info()
    exchange, account_address = make_exchange()
    open_orders = info.open_orders(account_address)
    coin_filter = _resolve_coin(args.coin) if args.coin else None
    to_cancel = [
        {"coin": o["coin"], "oid": o["oid"]}
        for o in open_orders
        if coin_filter is None or o["coin"] == coin_filter
    ]
    if not to_cancel:
        emit({"ok": True, "cancelled": 0, "message": "no open orders to cancel"})
        return
    request = {"action": "cancel_all", "coin_filter": coin_filter, "count": len(to_cancel)}
    with Timer() as t:
        try:
            response = exchange.bulk_cancel(to_cancel)
        except Exception as exc:  # noqa: BLE001
            log_attempt("cancel-all", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")
    log_attempt("cancel-all", request, response, t.elapsed_ms)
    emit({
        "ok": True,
        "cancelled": len(to_cancel),
        "request": request,
        "response": response,
        "latency_ms": round(t.elapsed_ms, 2),
    })


_TIF_MAP = {"gtc": "Gtc", "alo": "Alo", "ioc": "Ioc"}


def cmd_modify(args: argparse.Namespace) -> None:
    cfg = hl_config.load()
    info = make_info()
    coin = _resolve_coin(args.coin)
    is_buy = args.side == "buy"
    size, mid = _resolve_size(info, coin, args.size, args.usd)
    limit_px = float(args.price)
    tif = _TIF_MAP[args.tif]

    try:
        check_notional(size, limit_px, cfg)
        check_slippage(limit_px, mid, is_buy, cfg)
    except PreflightError as exc:
        fail("preflight_error", str(exc))

    exchange, _ = make_exchange()
    request = {
        "action": "modify",
        "oid": args.order_id,
        "coin": coin,
        "is_buy": is_buy,
        "sz": size,
        "limit_px": limit_px,
        "tif": tif,
        "reduce_only": args.reduce_only,
    }

    if args.preview:
        emit({"ok": True, "preview": True, "request": request})
        return

    with Timer() as t:
        try:
            response = exchange.modify_order(
                oid=int(args.order_id),
                name=coin,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_px,
                order_type={"limit": {"tif": tif}},
                reduce_only=args.reduce_only,
            )
        except Exception as exc:  # noqa: BLE001
            log_attempt("modify", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")
    log_attempt("modify", request, response, t.elapsed_ms)
    emit({"ok": True, "request": request, "response": response, "latency_ms": round(t.elapsed_ms, 2)})


def cmd_set_leverage(args: argparse.Namespace) -> None:
    cfg = hl_config.load()
    coin = _resolve_coin(args.coin)
    leverage = int(args.leverage)
    try:
        check_leverage(leverage, cfg)
    except PreflightError as exc:
        fail("preflight_error", str(exc))

    # margin mode: CLI flag overrides config default
    if args.isolated:
        is_cross = False
    elif args.cross:
        is_cross = True
    else:
        is_cross = cfg.default_margin_mode == "cross"

    exchange, _ = make_exchange()
    request = {"action": "set-leverage", "coin": coin, "leverage": leverage, "is_cross": is_cross}
    with Timer() as t:
        try:
            response = exchange.update_leverage(leverage, coin, is_cross)
        except Exception as exc:  # noqa: BLE001
            log_attempt("set-leverage", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")
        if isinstance(response, dict) and response.get("status") == "err":
            msg = response.get("response") or str(response)
            log_attempt("set-leverage", request, response, t.elapsed, msg)
            fail("execution_error", f"set-leverage rejected by exchange: {msg}")
    log_attempt("set-leverage", request, response, t.elapsed_ms)
    ok = bool(response) and (not isinstance(response, dict) or response.get("status") != "err")
    emit({"ok": ok, "request": request, "response": response, "latency_ms": round(t.elapsed_ms, 2)})


def cmd_bracket(args: argparse.Namespace) -> None:
    """Open a position with stop-loss and take-profit placed atomically.

    Uses HL's bulk_orders with grouping='positionTpsl' so the three orders
    (entry + stop + TP) are submitted in a single signed request.

    Guard-rails:
      - Entry direction: buy → stop<entry<tp; sell → tp<entry<stop
      - Stop is a trigger sell reduce-only (long) or buy reduce-only (short)
      - TP is the opposite side, also reduce-only
    """
    cfg = hl_config.load()
    info = make_info()
    coin = _resolve_coin(args.coin)
    is_buy = args.side == "buy"

    size, mid = _resolve_size(info, coin, args.size, args.usd)

    # Entry price: market uses aggressive IOC; limit uses user-supplied price
    if args.entry_type == "market":
        entry_px = _market_price(mid, is_buy, cfg.slippage_bps)
        entry_order_type = {"limit": {"tif": "Ioc"}}
        entry_tif = "Ioc"
    else:
        if args.entry_price is None:
            fail("validation", "--entry-price is required for --entry-type limit")
        entry_px = float(args.entry_price)
        entry_order_type = {"limit": {"tif": "Gtc"}}
        entry_tif = "Gtc"

    stop_px = float(args.stop_loss)
    tp_px = float(args.take_profit)

    # Sanity: stop/tp on correct side of entry
    if is_buy:
        if not (stop_px < entry_px < tp_px):
            fail("validation", f"for BUY need stop({stop_px}) < entry({entry_px}) < tp({tp_px})")
    else:
        if not (tp_px < entry_px < stop_px):
            fail("validation", f"for SELL need tp({tp_px}) < entry({entry_px}) < stop({stop_px})")

    # Preflight on the entry only — the exit legs are reduce-only same size
    try:
        check_leverage(args.leverage, cfg)
        check_notional(size, entry_px, cfg)
        check_limit_for_large(size, entry_px, entry_tif, cfg)
        check_slippage(entry_px, mid, is_buy, cfg)
        fund_ctx = check_funding(info, coin, is_buy, cfg) if is_buy else None
    except PreflightError as exc:
        fail("preflight_error", str(exc))

    # Exit leg price: for isMarket=True trigger orders, limit_px must be aggressive
    # enough to guarantee fill when the trigger fires. We use a wide buffer on the
    # correct side of the trigger.
    buffer_bps = cfg.slippage_bps * 3  # 3x slippage for safety on triggered fills
    buf = buffer_bps / 10_000
    stop_limit_px = stop_px * (1 - buf) if is_buy else stop_px * (1 + buf)
    tp_limit_px = tp_px * (1 + buf) if is_buy else tp_px * (1 - buf)

    cloid_entry = _new_cloid()
    cloid_stop = _new_cloid()
    cloid_tp = _new_cloid()

    order_requests = [
        {
            "coin": coin,
            "is_buy": is_buy,
            "sz": size,
            "limit_px": entry_px,
            "order_type": entry_order_type,
            "reduce_only": False,
            "cloid": Cloid.from_str(cloid_entry),
        },
        {
            "coin": coin,
            "is_buy": not is_buy,  # stop exits on the opposite side
            "sz": size,
            "limit_px": stop_limit_px,
            "order_type": {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
            "cloid": Cloid.from_str(cloid_stop),
        },
        {
            "coin": coin,
            "is_buy": not is_buy,
            "sz": size,
            "limit_px": tp_limit_px,
            "order_type": {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
            "reduce_only": True,
            "cloid": Cloid.from_str(cloid_tp),
        },
    ]

    request = {
        "action": "bracket",
        "coin": coin,
        "side": args.side,
        "size": size,
        "entry_px": entry_px,
        "entry_type": args.entry_type,
        "stop_px": stop_px,
        "tp_px": tp_px,
        "leverage": args.leverage,
        "mid_at_submit": mid,
        "notional_usd": round(size * entry_px, 4),
        "cloids": {"entry": cloid_entry, "stop": cloid_stop, "tp": cloid_tp},
        "risk_reward_ratio": round(abs(tp_px - entry_px) / abs(entry_px - stop_px), 3),
    }

    if args.preview:
        emit({"ok": True, "preview": True, "request": request, "funding_ctx": fund_ctx})
        return

    exchange, _ = make_exchange()

    is_cross = cfg.default_margin_mode == "cross"
    _ensure_leverage(exchange, coin, args.leverage, is_cross)

    with Timer() as t:
        try:
            # grouping="positionTpsl" makes the three orders atomic w.r.t. this position
            response = exchange.bulk_orders(order_requests, grouping="positionTpsl")
        except Exception as exc:  # noqa: BLE001
            log_attempt("bracket", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")

    log_attempt("bracket", request, response, t.elapsed_ms)

    # Response contains a statuses array with one entry per order request
    parsed_legs = []
    try:
        statuses = response["response"]["data"]["statuses"]
        for i, s in enumerate(statuses):
            leg_name = ["entry", "stop", "tp"][i] if i < 3 else f"leg_{i}"
            if "resting" in s:
                parsed_legs.append({"leg": leg_name, "status": "resting", "oid": s["resting"].get("oid")})
            elif "filled" in s:
                parsed_legs.append({
                    "leg": leg_name,
                    "status": "filled",
                    "oid": s["filled"].get("oid"),
                    "filled": s["filled"],
                })
            elif "error" in s:
                parsed_legs.append({"leg": leg_name, "status": "error", "error": s["error"]})
    except (KeyError, TypeError, IndexError):
        parsed_legs = []

    # Orphan cleanup: if entry failed, exit legs would dangle as resting reduce-only
    # orders against a non-existent position. Cancel them proactively.
    cleanup = _cleanup_orphan_legs(exchange, coin, parsed_legs)

    emit({
        "ok": True,
        "request": request,
        "response": response,
        "parsed_legs": parsed_legs,
        "funding_ctx": fund_ctx,
        "orphan_cleanup": cleanup,
        "latency_ms": round(t.elapsed_ms, 2),
    })


def _cleanup_orphan_legs(exchange, coin: str, parsed_legs: list[dict]) -> dict | None:
    """If the entry leg errored, cancel any resting stop/tp legs to avoid orphans.

    Defensive belt-and-suspenders: HL's `bulk_orders(grouping="positionTpsl")`
    is documented as atomic — if entry fails the exit legs should not be
    accepted either. This guard catches behavioral drift (or future grouping
    changes) so we never leave orphan reduce-only triggers against a position
    that doesn't exist.

    Returns None if no cleanup was needed, else a summary of cancellation attempts.
    """
    entry_leg = next((leg for leg in parsed_legs if leg.get("leg") == "entry"), None)
    if not entry_leg or entry_leg.get("status") != "error":
        return None

    to_cancel = [
        {"coin": coin, "oid": leg["oid"]}
        for leg in parsed_legs
        if leg.get("leg") in ("stop", "tp") and leg.get("status") == "resting" and leg.get("oid")
    ]
    if not to_cancel:
        return {"reason": "entry errored; no resting exit legs to cancel"}

    request = {"action": "cancel_orphans", "coin": coin, "oids": [c["oid"] for c in to_cancel]}
    with Timer() as t:
        try:
            response = exchange.bulk_cancel(to_cancel)
        except Exception as exc:  # noqa: BLE001
            log_attempt("cancel-orphans", request, None, t.elapsed, str(exc))
            return {"cancelled": 0, "error": str(exc), "attempted": request["oids"]}
    log_attempt("cancel-orphans", request, response, t.elapsed_ms)
    return {"cancelled": len(to_cancel), "oids": request["oids"], "response": response}


def cmd_close(args: argparse.Namespace) -> None:
    """Close a position via a reduce-only IOC at aggressive price.

    --size-pct N (default 100) closes that fraction of the current position.

    Preflight note: no leverage / notional / slippage guards here — the
    order is reduce-only (notional bounded by the position) and the IOC
    price is `_market_price(mid, ...)` which by construction stays inside
    `check_slippage`'s band. The remaining risk is mid moving between
    price computation and order arrival; that's a property of the IOC
    execution model, not something preflight can guard against.
    """
    cfg = hl_config.load()
    info = make_info()
    exchange, account_address = make_exchange()
    coin = _resolve_coin(args.coin)

    if args.size_pct <= 0 or args.size_pct > 100:
        fail("validation", f"--size-pct must be in (0, 100], got {args.size_pct}")

    state = info.user_state(account_address)
    position = None
    for p in state.get("assetPositions", []):
        if p["position"]["coin"] == coin:
            position = p["position"]
            break
    if position is None:
        fail("validation", f"no open position in {coin!r}")

    current_size = float(position["szi"])  # signed
    if current_size == 0:
        fail("validation", f"position in {coin!r} is zero")
    is_long = current_size > 0

    sz_dec = get_sz_decimals(info, coin)
    close_size = round_size(abs(current_size) * args.size_pct / 100, sz_dec)
    if close_size <= 0:
        fail("validation", f"close size rounded to zero at szDecimals={sz_dec}")

    dex = dex_for(coin)
    mids = info.all_mids() if dex is None else info.post("/info", {"type": "allMids", "dex": dex})
    mid = float(mids[coin])
    # close = sell if long, buy if short; use aggressive IOC price
    is_buy_side = not is_long
    limit_px = _market_price(mid, is_buy_side, cfg.slippage_bps)

    cloid = _new_cloid()
    request = {
        "action": "close",
        "coin": coin,
        "is_buy": is_buy_side,
        "size": close_size,
        "size_pct": args.size_pct,
        "limit_px": limit_px,
        "mid_at_submit": mid,
        "cloid": cloid,
    }

    if args.preview:
        emit({"ok": True, "preview": True, "request": request})
        return

    with Timer() as t:
        try:
            response = exchange.order(
                name=coin,
                is_buy=is_buy_side,
                sz=close_size,
                limit_px=limit_px,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=True,
                cloid=Cloid.from_str(cloid),
            )
        except Exception as exc:  # noqa: BLE001
            log_attempt("close", request, None, t.elapsed, str(exc))
            fail("execution_error", f"{type(exc).__name__}: {exc}")
    log_attempt("close", request, response, t.elapsed_ms)
    parsed = _parse_order_response(response, cloid)
    emit({"ok": True, "request": request, "response": response, "parsed": parsed, "latency_ms": round(t.elapsed_ms, 2)})


# --- Argparse registration ---

def register_order_subparsers(sub: argparse._SubParsersAction) -> None:
    # hl order buy|sell COIN [SIZE] [--usd N] [flags...]
    o = sub.add_parser("order", help="Place an order (dangerous)")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("coin", help="Coin symbol (e.g. BTC, xyz:CL)")
    o.add_argument("size", type=float, nargs="?", help="Size in coin units (alternative: --usd)")
    o.add_argument("--usd", type=float, help="Size specified as USD notional (converted via mid)")
    o.add_argument("--type", choices=["limit", "market"], default="limit")
    o.add_argument("--price", help="Limit price (required for --type limit)")
    o.add_argument("--leverage", type=int, default=1)
    o.add_argument("--reduce-only", action="store_true")
    o.add_argument("--post-only", action="store_true", help="Alo — reject if would cross")
    o.add_argument("--ioc", action="store_true", help="Immediate-or-cancel")
    o.add_argument("--fok", action="store_true", help="(unsupported on HL — errors)")
    o.add_argument("--trigger-px", type=float, help="Trigger price for a stop/TP order")
    o.add_argument("--tpsl", choices=["tp", "sl"], help="Trigger order kind (required with --trigger-px)")
    o.add_argument("--client-order-id", help="Custom cloid (default: generated UUID)")
    o.add_argument("--preview", action="store_true", help="Print signed payload without submitting")
    o.set_defaults(func=cmd_order)

    c = sub.add_parser("cancel", help="Cancel an order by id (dangerous)")
    c.add_argument("coin", help="Coin symbol")
    c.add_argument("--order-id", required=True)
    c.set_defaults(func=cmd_cancel)

    ca = sub.add_parser("cancel-all", help="Cancel all open orders (optionally scoped by coin) (dangerous)")
    ca.add_argument("--coin", help="Only cancel orders for this coin")
    ca.set_defaults(func=cmd_cancel_all)

    m = sub.add_parser("modify", help="Modify a live order (dangerous)")
    m.add_argument("side", choices=["buy", "sell"])
    m.add_argument("coin")
    m.add_argument("size", type=float, nargs="?")
    m.add_argument("--usd", type=float)
    m.add_argument("--price", required=True)
    m.add_argument("--order-id", required=True)
    m.add_argument("--reduce-only", action="store_true")
    m.add_argument(
        "--tif",
        choices=["gtc", "alo", "ioc"],
        default="gtc",
        help="Time-in-force for the modified order. HL's modify call requires the new TIF "
        "explicitly (it's not preserved from the original). Default gtc — pass alo or ioc "
        "if you're modifying an existing post-only or IOC order.",
    )
    m.add_argument("--preview", action="store_true")
    m.set_defaults(func=cmd_modify)

    sl = sub.add_parser("set-leverage", help="Set leverage + margin mode (dangerous)")
    sl.add_argument("coin")
    sl.add_argument("leverage", type=int)
    grp = sl.add_mutually_exclusive_group()
    grp.add_argument("--isolated", action="store_true")
    grp.add_argument("--cross", action="store_true")
    sl.set_defaults(func=cmd_set_leverage)

    cl = sub.add_parser("close", help="Reduce-only close of an open position (dangerous)")
    cl.add_argument("coin")
    cl.add_argument("--size-pct", type=float, default=100.0, help="Fraction of position to close (default 100)")
    cl.add_argument("--preview", action="store_true")
    cl.set_defaults(func=cmd_close)

    br = sub.add_parser("bracket", help="Atomic entry + stop-loss + take-profit (dangerous)")
    br.add_argument("side", choices=["buy", "sell"])
    br.add_argument("coin")
    br.add_argument("size", type=float, nargs="?")
    br.add_argument("--usd", type=float)
    br.add_argument("--entry-type", choices=["limit", "market"], default="market")
    br.add_argument("--entry-price", type=float, help="Required for --entry-type limit")
    br.add_argument("--stop-loss", type=float, required=True)
    br.add_argument("--take-profit", type=float, required=True)
    br.add_argument("--leverage", type=int, default=1)
    br.add_argument("--preview", action="store_true")
    br.set_defaults(func=cmd_bracket)
