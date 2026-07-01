# hyperliquid-cli

Standalone CLI for the [Hyperliquid](https://hyperliquid.xyz) perps DEX. Read-only market/account data and signed execution via API wallet — including atomic entry + stop-loss + take-profit ("bracket") orders.

Built on top of the official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk). No adapter layer, no orchestrator, no upstream project to share with — just the SDK and a thin JSON-emitting CLI around it.

## Features

- **Read-only** (no auth): mid prices, orderbook, OHLC, funding history, meta, portfolio
- **Account reads** (address only): user state, open orders, fills, equity curve
- **Execution** (API wallet): limit / market / trigger orders, cancel / modify / set-leverage / reduce-only close
- **Atomic bracket orders** (`hl bracket`): entry + stop-loss + take-profit in one signed request
- **HIP-3 deployer markets** (`xyz:CL`, `cash:WTI`, `km:US500`, …) — first-class, pass `xyz:CL` directly
- **Safety layer**: leverage cap, notional cap, slippage check, funding-rate guard, main-key detection
- **Stable JSON envelope**: every command emits one JSON document on stdout, exits 0 on success / 1 on failure
- **Order-attempt log**: append-only JSONL under `logs/hl-execution/`

## Install

```bash
pip install hyperliquid-cli
```

…or with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install hyperliquid-cli
```

Then:

```bash
hl --help
```

## Quick start

Public market data — no credentials needed:

```bash
hl ticker BTC                       # mid price for BTC
hl all-mids                         # every tradeable coin
hl meta                             # perp universe (symbols, max leverage)
hl orderbook ETH --depth 5          # L2 orderbook, top 5 levels per side
hl ohlc SOL --interval 1h --lookback 100
hl funding ETH --lookback 24        # 24 hours of funding rates
hl meta-ctx                         # universe + mark/oracle/funding/OI per asset
```

HIP-3 deployer markets work with the same commands — just pass `dex:COIN`:

```bash
hl ticker xyz:CL
hl funding cash:WTI --lookback 12
hl orderbook km:US500 --depth 3
```

Account reads need the main wallet's public address:

```bash
export HL_ACCOUNT_ADDRESS=0xYourAddress
hl user-state                       # account value, margin, positions
hl open-orders                      # resting orders
hl user-fills --lookback 24         # fills in the last 24 hours
hl portfolio                        # equity curve + PnL buckets
```

## Execution

Execution uses Hyperliquid's **API wallet** model — the main wallet's private key never enters this process. Generate an API wallet at <https://app.hyperliquid.xyz/API>, approve it from your main wallet (one-time, gas-free), then:

```bash
export HL_ACCOUNT_ADDRESS=0xYourMainAddress
export HL_SECRET_KEY=0xYourApiWalletPrivateKey
```

Place orders:

```bash
# Limit order, 0.01 BTC at $50,000
hl order buy BTC 0.01 --price 50000

# Market order, $200 notional (converted via mid + szDecimals)
hl order buy BTC --usd 200

# Reduce-only market close of an existing long
hl close BTC

# Stop-loss (trigger) — typically with --reduce-only
hl order sell BTC 0.01 --trigger-px 45000 --tpsl sl --reduce-only
```

Atomic bracket (entry + stop + take-profit in one signed request):

```bash
hl bracket buy BTC 0.01 --stop-loss 45000 --take-profit 75000
hl bracket buy xyz:CL --usd 50 --stop-loss 65 --take-profit 80
```

Modify / cancel:

```bash
hl cancel BTC --order-id 12345
hl cancel-all                          # all coins
hl cancel-all --coin BTC                # one coin
hl modify buy BTC 0.01 --price 51000 --order-id 12345 --tif gtc
hl set-leverage BTC 1 --isolated
```

Dry-run every execution command with `--preview`:

```bash
hl bracket buy xyz:CL --usd 10 --stop-loss 65 --take-profit 80 --preview
```

…which prints the full signed payload (entry/stop/tp legs with cloids, funding context, notional) without touching the network.

## Environment variables

| Variable              | Required for               | Default     | Purpose                                                  |
| --------------------- | -------------------------- | ----------- | -------------------------------------------------------- |
| `HL_ACCOUNT_ADDRESS`  | account queries, execution | —           | Main wallet public address (not secret)                  |
| `HL_SECRET_KEY`       | execution                  | —           | API wallet private key (secret, scoped to order-signing) |
| `HL_MAX_LEVERAGE`     | execution                  | `1`         | Hard upper bound on `--leverage`                         |
| `HL_MAX_NOTIONAL_USD` | execution                  | `200`       | Per-order USD cap                                        |
| `HL_SLIPPAGE_BPS`     | execution                  | `100` (1%)  | Limit price must be within this of mid                   |
| `HL_MAX_FUNDING_PCT`  | execution                  | `50` (%/yr) | Refuse new longs above this; longs only; `0` disables    |
| `HL_MARGIN_MODE`      | execution                  | `isolated`  | Default margin mode for `set-leverage`                   |

## Safety

- **Main-key guard**: at startup, `hl` derives the address from `HL_SECRET_KEY`. If it matches `HL_ACCOUNT_ADDRESS`, the CLI refuses to run with a structured `auth` error. This catches the "user pasted main key into the API slot" footgun.
- **API wallet**: cannot withdraw, transfer, or move funds off the exchange. Revocable from the web UI. The main wallet's key never enters the process.
- **Preflight checks**: every signed command validates leverage, notional, slippage, and (for longs) funding rate before any SDK call.
- **`--preview`**: dry-run mode that prints the signed payload as JSON. Use it to verify a bracket before committing margin.
- **Atomic brackets**: `hl bracket` submits entry + stop + take-profit as one signed request (`bulk_orders(grouping="positionTpsl")`). If the entry leg fails, the CLI defensively cancels any resting exit legs to prevent orphan reduce-only triggers.
- **Order-attempt log**: every signed command appends one JSONL line to `logs/hl-execution/{YYYY-MM-DD}.jsonl` with the request, response, latency, and PID — auditable history of every action.

## Error envelope

Every command emits one JSON document on stdout. Exit code is `0` on success, `1` on failure.

Success — the SDK response verbatim:

```json
{ "coin": "BTC", "mid": 58863.5 }
```

Failure — a stable envelope you can branch on programmatically:

```json
{
  "ok": false,
  "error": "preflight_error",
  "message": "notional $594.71 exceeds max_notional_usd $200.00"
}
```

Categories: `api`, `network`, `rate_limit`, `validation`, `auth`, `config`, `io`, `parse`, `execution_error`, `preflight_error`. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#error-envelope) for the full table.

## Development

```bash
git clone git@github.com:krzysu/hyperliquid-cli.git
cd hyperliquid-cli
uv sync                  # install dev deps
uv run hl --help
uv run pytest            # 86 tests
uv run ruff check hl/ tests/
```

To install the local checkout as a tool:

```bash
uv tool install .
hl --help
```

## See also

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design notes, package layout, safety-layer details, error envelope, extension points
- [Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) — the upstream SDK this CLI wraps
- [Hyperliquid API docs](https://hyperliquid.gitbook.io/hyperliquid-docs) — the underlying REST/WebSocket API

## License

MIT — see [`LICENSE`](LICENSE).
