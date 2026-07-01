# Architecture

Design notes for `hyperliquid-cli`. What the package does, why it's shaped that way, and the contracts between its components.

## Goals

- **Standalone.** No upstream project, no orchestrator, no adapter layer. The SDK is the only runtime dependency.
- **Read-only by default.** All market-data commands work without any credentials.
- **Safe execution.** Every signed command goes through a defense-in-depth safety layer before reaching the SDK.
- **Machine-friendly output.** Every command emits a single JSON document on stdout. Errors are structured envelopes with a stable `error` category.
- **Atomic bracket orders.** Entry + stop-loss + take-profit submitted in one signed request via the SDK's `bulk_orders(grouping="positionTpsl")`.

## Two clients, one CLI

Hyperliquid's SDK exposes two clients:

- `hyperliquid.info.Info` — read-only, address-scoped queries (`user_state`, `open_orders`, `user_fills`, `portfolio`, `meta`, `candles_snapshot`, `l2_snapshot`, `funding_history`, `all_mids`, `meta_and_asset_ctxs`).
- `hyperliquid.exchange.Exchange` — signing client. Wraps an `eth_account.LocalAccount` and submits actions (orders, cancels, leverage updates, bulk orders).

`hyperliquid-cli` mirrors that split:

| Stage         | Module         | SDK client | Auth                         |
| ------------- | -------------- | ---------- | ---------------------------- |
| 1 — read-only | `hl/cli.py`    | `Info`     | none / address-only          |
| 2 — execution | `hl/orders.py` | `Exchange` | API wallet (`HL_SECRET_KEY`) |

Both clients connect to the SDK's `MAINNET_API_URL` constant. There is no testnet mode (the project ships against mainnet only by design).

## Wallet model

Hyperliquid distinguishes two wallet roles:

| Role        | Holds USDC | Signs orders                     | Can withdraw |
| ----------- | ---------- | -------------------------------- | ------------ |
| Main wallet | ✓          | ✓                                | ✓            |
| API wallet  | —          | ✓ (orders/cancels/leverage only) | —            |

`hyperliquid-cli` uses the **API wallet** model. The main wallet key never enters the process. The user generates an API wallet at <https://app.hyperliquid.xyz/API>, approves it from the main wallet (one-time, gas-free), and exposes:

- `HL_ACCOUNT_ADDRESS` — main wallet public address (not secret)
- `HL_SECRET_KEY` — API wallet private key (secret, scoped to order-signing)

### Main-key guard

`hl/auth.py:load_wallet` derives the address from `HL_SECRET_KEY` and refuses to start if it matches `HL_ACCOUNT_ADDRESS`. That catches the "user pasted their main key into the API slot" footgun — which would otherwise expose withdraw authority to this CLI. The error is a structured `auth` envelope, not a stack trace.

The guard fires on the first `make_exchange()` call. Commands that only call `make_info()` (read-only, `--preview` paths) do not trigger the guard — preview is intentionally cheap and key-free.

## Safety layer

Every signed command runs through `hl/safety.py` preflight guards before any network call. All guards raise `PreflightError`, which the CLI maps to a `preflight_error` envelope and exits non-zero.

| Guard                         | Default    | Purpose                                                                 |
| ----------------------------- | ---------- | ----------------------------------------------------------------------- |
| `max_leverage`                | 1          | Forbid anything >1x by default. Override per-order with `--leverage`.   |
| `max_notional_usd`            | 200        | Per-order USD cap. Catches fat-finger typos.                            |
| `slippage_bps`                | 100 (1%)   | Limit price must be within N bps of mid on the aggressive side.         |
| `require_limit_for_large_usd` | 100        | Notionals >$100 must be limit (GTC) — no market-order slippage on size. |
| `max_funding_pct_annualized`  | 50 (%/yr)  | Refuse new longs when crowd-long drag is extreme. `0` disables.         |
| `default_margin_mode`         | `isolated` | Used by `set-leverage` if `--isolated` / `--cross` not passed.          |

Defaults are in `hl/config.py` (`SafetyConfig`). Override via `HL_*` env vars.

### Runtime guarantees

- `cmd_order` and `cmd_bracket` call `_ensure_leverage` after preflight and **before** the order placement. If `update_leverage` raises **or returns `{"status": "err", ...}`**, the order is aborted with an `execution_error` — we never place an order at unknown leverage.
- `cmd_bracket` uses `bulk_orders(grouping="positionTpsl")` so the three legs are atomic w.r.t. the position. After submission, `_cleanup_orphan_legs` runs defensively: if the entry leg errored but the stop/tp legs are resting, it cancels them so no orphan reduce-only triggers sit against a position that doesn't exist.
- `cmd_close` deliberately skips the `check_notional` / `check_limit_for_large` / `check_slippage` guards: the order is reduce-only (notional bounded by the position) and the IOC price is computed via `_market_price(mid, ...)` which by construction stays inside the slippage band. The remaining risk — mid moving between price computation and order arrival — is a property of the IOC execution model, not something preflight can guard against.
- The `--preview` flag short-circuits every command before the SDK call and prints the signed payload (request shape, funding context, cloid) as JSON. Use it to verify a bracket before committing margin.

## HIP-3 deployer markets

HIP-3 deployers (`xyz`, `cash`, `km`, …) publish their own perps on Hyperliquid with the same `/info` and `/exchange` endpoints — distinguished by a `dex` parameter and a `dex:COIN` symbol.

`hyperliquid-cli` treats `dex:COIN` as a first-class symbol everywhere:

- `hl ticker xyz:CL`, `hl funding xyz:CL --lookback 24`, `hl orderbook xyz:CL --depth 5`, `hl ohlc xyz:CL --interval 1h`
- `hl order buy xyz:CL --usd 10 --price 68`
- `hl bracket sell xyz:CL --usd 10 --stop-loss 75 --take-profit 60`

The router logic lives in `hl/sizing.py`:

- `dex_for("xyz:CL")` → `"xyz"`
- `dex_for("BTC")` → `None` (native perps)
- `get_sz_decimals(info, "xyz:CL")` → `info.post("/info", {"type": "meta", "dex": "xyz"})` then look up the asset's `szDecimals`

Pair normalization (`hl/sizing.py:_split_pair`) accepts `BTCUSD`, `ETH/USDC`, `xyz:CL`, `XYZ:CL` and resolves each to the canonical HL coin name on the wire. The CLI does not validate against the universe — an unknown coin surfaces as `{"ok": false, "error": "validation", ...}`.

## Error envelope

Every command emits one JSON document on stdout. Exit code is `0` on success, `1` on failure.

Success: the SDK's response payload verbatim, e.g.

```json
{ "coin": "BTC", "mid": 58863.5 }
```

Failure: a stable envelope

```json
{ "ok": false, "error": "<category>", "message": "<str>" }
```

Categories:

| Category          | Source                                                                               |
| ----------------- | ------------------------------------------------------------------------------------ |
| `api`             | SDK or HTTP error that doesn't fit elsewhere                                         |
| `network`         | connection / timeout                                                                 |
| `rate_limit`      | HTTP 429                                                                             |
| `validation`      | bad user input (malformed address, bad size, unknown coin, mutually-exclusive flags) |
| `auth`            | main-key guard fired / wrong-length secret                                           |
| `config`          | required env var missing                                                             |
| `io`              | I/O or interruption                                                                  |
| `parse`           | unparseable response shape                                                           |
| `execution_error` | HL rejected the order (insufficient margin, min-size, post-only crossed, …)          |
| `preflight_error` | local safety guard blocked (leverage / notional / slippage / funding)                |

`preflight_error` and `execution_error` are the two categories unique to Stage 2. They make it possible to programmatically distinguish "this command would have been unsafe — fix the inputs" from "the command was well-formed but the exchange rejected it".

## Order logging

Every signed command appends one JSONL line to `logs/hl-execution/{YYYY-MM-DD}.jsonl` (the `logs/` directory is gitignored). Each line contains:

```json
{
  "ts": "2026-07-01T09:39:22.468989+00:00",
  "action": "order",
  "request": { ...signed payload... },
  "response": { ...HL response... },
  "error": null,
  "latency_ms": 327.82,
  "pid": 26831
}
```

Latency is captured by a `Timer` context manager (`hl/order_logger.py:Timer`) whose `elapsed` property is readable mid-block — important for the failure path, which logs latency _inside_ the `except` block before re-raising. The test `tests/test_order_logger.py::test_elapsed_in_except` locks this behavior in.

The test suite redirects `_LOG_ROOT` to a per-test temp dir via an autouse fixture (`tests/conftest.py`) so tests never pollute the operational log tree.

## Size semantics

HL's API takes order size in **coin units** (`BTC 0.01`), not USD notional. `hyperliquid-cli` accepts both:

```bash
hl order buy BTC 0.01                  # native coin units
hl order buy BTC --usd 500             # converted via current mid
```

Conversion goes through `hl/sizing.py`:

1. `usd_to_size(usd, mid, sz_decimals)` → `usd / mid`, rounded **down** to the asset's `szDecimals` to avoid min-size rejection
2. `round_size(size, sz_decimals)` → floor to lot size (e.g. `0.12345` at 4 decimals → `0.1234`)

The CLI rejects sizes that round to zero (e.g. `$0.01` of BTC rounds to 0 at 4 decimals) with a `validation` envelope.

## Atomic bracket orders

`hl bracket` is the headline feature of Stage 2. It builds three orders and submits them via the SDK's `bulk_orders(grouping="positionTpsl")`:

1. **Entry** — limit at user-supplied price (GTC) or aggressive IOC at mid (market entry)
2. **Stop-loss** — `trigger{tpsl:"sl"}` on the opposite side, reduce-only
3. **Take-profit** — `trigger{tpsl:"tp"}` on the opposite side, reduce-only

`positionTpsl` grouping makes the three legs atomic w.r.t. the resulting position: if entry fills, both triggers attach; if entry doesn't fill, neither trigger attaches.

The CLI validates the relative price geometry _before_ preflight:

- BUY: `stop < entry < tp`
- SELL: `tp < entry < stop`

…and uses the existing safety layer (leverage / notional / slippage / funding) on the entry leg only. Exit legs are reduce-only at the same size — their notional is bounded by the entry.

After submission, `_cleanup_orphan_legs` runs defensively: if the entry leg errored but the stop/tp legs are resting, it cancels them so no orphan reduce-only triggers sit against a position that doesn't exist. This is belt-and-suspenders — `positionTpsl` is documented as atomic, but the guard catches behavioral drift or future SDK changes.

## Extension points

To add a new read-only command (Stage 1):

1. Add a `cmd_xxx` function in `hl/cli.py` that calls the appropriate `Info` method
2. Wrap the call in `try/except` (the top-level handler in `main()` already categorizes exceptions)
3. Register the subparser in `build_parser()` with `set_defaults(func=cmd_xxx)`
4. Add a test under `tests/` (use `MagicMock` for the SDK — no network in tests)

To add a new signed command (Stage 2):

1. Add a `cmd_xxx` function in `hl/orders.py`
2. Load `hl_config.load()`, call `make_info()` for preflight reads, call `make_exchange()` for the actual signing
3. Wrap preflight in `try/except PreflightError → fail("preflight_error", ...)`
4. Wrap the SDK call in `try/except → fail("execution_error", ...)` and log via `log_attempt`
5. Support `--preview` — print the signed payload as JSON and return without touching the network
6. Register the subparser in `register_order_subparsers()`
7. Add tests covering both the happy path (mocked SDK) and the validation/error paths (envelopes)

## Deliberate non-features

- **No withdraw / transfer / vault commands.** Those require the main wallet key and are out of scope. Use the Hyperliquid web UI.
- **No testnet.** The project ships against mainnet only by user direction. The SDK's `TESTNET_API_URL` is available if a future contributor wants to add a `--network testnet` flag.
- **No paper mode.** There is no HL paper account. Use `--preview` for dry runs.
- **No websocket streaming.** The SDK ships a websocket manager; this CLI is request/response only.
- **No HL spot.** Spot uses `@<N>` index symbols; deferred.
