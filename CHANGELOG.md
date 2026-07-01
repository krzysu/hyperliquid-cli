# Changelog

All notable changes to `hyperliquid-cli` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-01

### Added

- **Read-only commands** (no auth): `ticker`, `all-mids`, `meta`, `meta-ctx`, `orderbook`, `ohlc`, `funding`, `user-state`, `open-orders`, `user-fills`, `portfolio`
- **Execution commands** (API wallet): `order`, `bracket`, `cancel`, `cancel-all`, `modify`, `set-leverage`, `close`
- **Atomic bracket orders** via `bulk_orders(grouping="positionTpsl")` — entry + stop-loss + take-profit in one signed request
- **HIP-3 deployer market support** — `xyz:CL`, `cash:WTI`, `km:US500`, etc. work with every command
- **USD ↔ coin-unit conversion** via `--usd` (rounds down to `szDecimals`)
- **Pair normalization** — accepts `BTCUSD`, `ETH/USDC`, `xyz:CL`, `XYZ:CL`
- **Safety layer**: leverage cap (default 1x), notional cap ($200), slippage check (1%), funding-rate guard (50%/yr for longs)
- **Main-key guard** — refuses to run if `HL_SECRET_KEY` derives to `HL_ACCOUNT_ADDRESS`
- **Order-attempt log** — append-only JSONL under `logs/hl-execution/{YYYY-MM-DD}.jsonl`
- **`--preview`** on every execution command for dry runs
- **Stable JSON envelope** — single JSON document on stdout, exit 0/1, error categories: `api`, `network`, `rate_limit`, `validation`, `auth`, `config`, `io`, `parse`, `execution_error`, `preflight_error`
- **86 tests** covering auth, safety, sizing, order logic, bracket shape, response parsing, logger timing

### Security

- API wallet model — main wallet key never enters the process
- `set-leverage` failure aborts order placement (no order at unknown leverage)
- Bracket cleanup on entry error — no orphan reduce-only triggers against non-existent positions
- `logs/` gitignored
- `.env*` gitignored

[0.1.0]: #010--2026-07-01
