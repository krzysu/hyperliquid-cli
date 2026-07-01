"""hl — Hyperliquid read-only CLI.

Wraps the official `hyperliquid-python-sdk` Info client and emits JSON.
Stage 1: public market data + address-scoped reads (no signing).
Stage 2 (later): signed order/cancel actions via an API wallet.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .envelope import categorize, emit, fail

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    "1M": 30 * 24 * 60 * 60_000,
}


def _info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _resolve_address(cli_address: str | None) -> str:
    addr = cli_address or os.environ.get("HL_ACCOUNT_ADDRESS")
    if not addr:
        fail(
            "config",
            "no address: pass --address or set HL_ACCOUNT_ADDRESS in the environment",
        )
    if not addr.startswith("0x") or len(addr) != 42:
        fail("validation", f"invalid EVM address: {addr!r}")
    return addr


def cmd_ticker(args: argparse.Namespace) -> None:
    coin = args.coin
    if ":" in coin:
        # HIP-3 deployer: route through /info with dex param
        dex = coin.split(":", 1)[0].lower()
        mids = _info().post("/info", {"type": "allMids", "dex": dex})
    else:
        mids = _info().all_mids()
    if coin not in mids:
        fail("validation", f"unknown coin {coin!r}; try `hl all-mids` to list symbols")
    emit({"coin": coin, "mid": float(mids[coin])})


def cmd_all_mids(args: argparse.Namespace) -> None:
    if args.dex:
        emit(_info().post("/info", {"type": "allMids", "dex": args.dex}))
    else:
        emit(_info().all_mids())


def cmd_meta(args: argparse.Namespace) -> None:
    emit(_info().meta())


def cmd_meta_ctx(args: argparse.Namespace) -> None:
    emit(_info().meta_and_asset_ctxs())


def cmd_orderbook(args: argparse.Namespace) -> None:
    book = _info().l2_snapshot(args.coin)
    if args.depth and book and "levels" in book:
        book["levels"] = [side[: args.depth] for side in book["levels"]]
    emit(book)


def cmd_ohlc(args: argparse.Namespace) -> None:
    step_ms = INTERVAL_MS[args.interval]
    end = int(time.time() * 1000)
    start = end - args.lookback * step_ms
    emit(_info().candles_snapshot(args.coin, args.interval, start, end))


def cmd_funding(args: argparse.Namespace) -> None:
    end = int(time.time() * 1000)
    start = end - args.lookback * 60 * 60_000  # --lookback is in hours
    emit(_info().funding_history(args.coin, start, end))


def cmd_user_state(args: argparse.Namespace) -> None:
    emit(_info().user_state(_resolve_address(args.address)))


def cmd_open_orders(args: argparse.Namespace) -> None:
    addr = _resolve_address(args.address)
    info = _info()
    emit(info.frontend_open_orders(addr) if args.frontend else info.open_orders(addr))


def cmd_user_fills(args: argparse.Namespace) -> None:
    addr = _resolve_address(args.address)
    info = _info()
    if args.lookback is not None:
        end = int(time.time() * 1000)
        start = end - args.lookback * 60 * 60_000
        emit(info.user_fills_by_time(addr, start, end))
    else:
        emit(info.user_fills(addr))


def cmd_portfolio(args: argparse.Namespace) -> None:
    emit(_info().portfolio(_resolve_address(args.address)))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hl",
        description="Hyperliquid read-only CLI. JSON output on stdout.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("ticker", help="Mid price for a single coin")
    t.add_argument("coin", help="Coin symbol (e.g. BTC, ETH, SOL)")
    t.set_defaults(func=cmd_ticker)

    am = sub.add_parser("all-mids", help="Mid prices for every tradeable coin")
    am.add_argument("--dex", help="HIP-3 deployer prefix (e.g. 'xyz') — else native perps")
    am.set_defaults(func=cmd_all_mids)

    m = sub.add_parser("meta", help="Perp universe (symbols, max leverage)")
    m.set_defaults(func=cmd_meta)

    mc = sub.add_parser(
        "meta-ctx",
        help="Perp universe + asset contexts (funding, mark, open interest)",
    )
    mc.set_defaults(func=cmd_meta_ctx)

    ob = sub.add_parser("orderbook", help="L2 orderbook snapshot")
    ob.add_argument("coin")
    ob.add_argument("--depth", type=int, default=0, help="Truncate to top N levels per side (0 = all)")
    ob.set_defaults(func=cmd_orderbook)

    oh = sub.add_parser("ohlc", help="Candles (OHLCV)")
    oh.add_argument("coin")
    oh.add_argument(
        "--interval",
        default="1h",
        choices=list(INTERVAL_MS.keys()),
        help="Candle interval",
    )
    oh.add_argument("--lookback", type=int, default=100, help="Number of candles back from now")
    oh.set_defaults(func=cmd_ohlc)

    fh = sub.add_parser("funding", help="Historical funding rates for a coin")
    fh.add_argument("coin")
    fh.add_argument("--lookback", type=int, default=24, help="Hours of history")
    fh.set_defaults(func=cmd_funding)

    us = sub.add_parser("user-state", help="Account value, margin, positions")
    us.add_argument("--address", help="Override HL_ACCOUNT_ADDRESS")
    us.set_defaults(func=cmd_user_state)

    oo = sub.add_parser("open-orders", help="Open orders for an address")
    oo.add_argument("--address", help="Override HL_ACCOUNT_ADDRESS")
    oo.add_argument(
        "--frontend",
        action="store_true",
        help="Use frontend_open_orders (includes trigger/TPSL metadata)",
    )
    oo.set_defaults(func=cmd_open_orders)

    uf = sub.add_parser("user-fills", help="Recent fills")
    uf.add_argument("--address", help="Override HL_ACCOUNT_ADDRESS")
    uf.add_argument("--lookback", type=int, help="Hours of history (default: most recent 2000 fills)")
    uf.set_defaults(func=cmd_user_fills)

    pf = sub.add_parser("portfolio", help="Account equity curve and PnL buckets")
    pf.add_argument("--address", help="Override HL_ACCOUNT_ADDRESS")
    pf.set_defaults(func=cmd_portfolio)

    # --- Stage 2: signed execution (order, cancel, modify, set-leverage, close) ---
    from .orders import register_order_subparsers

    register_order_subparsers(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        fail("io", "interrupted")
    except Exception as exc:  # noqa: BLE001
        category, message = categorize(exc)
        fail(category, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
