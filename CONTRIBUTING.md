# Contributing

Thanks for your interest in `hyperliquid-cli`. Issues, bug reports, and pull requests are welcome.

## Development setup

```bash
git clone git@github.com:krzysu/hyperliquid-cli.git
cd hyperliquid-cli
uv sync                       # install runtime + dev deps
uv run hl --help              # smoke-test the CLI
uv run pytest                 # run the test suite (86 tests)
uv run ruff check hl/ tests/  # lint
```

Requires Python 3.11+.

## Running tests

The suite is hermetic — no network calls, no mainnet credentials. Every test mocks the SDK.

```bash
uv run pytest                          # full suite
uv run pytest tests/test_orders.py     # one file
uv run pytest -k "bracket"             # by name
uv run pytest --tb=short -q            # terse tracebacks
```

The `tests/conftest.py` autouse fixture redirects the order-attempt log to a temp dir, so tests never pollute the operational log tree.

## Code style

- Lint with `ruff` (`uv run ruff check hl/ tests/`)
- Line length 120 (set in `pyproject.toml`)
- No comments unless they explain a non-obvious invariant
- Type hints on all public functions
- Module-level docstrings on every `.py` file

## Pull request checklist

- [ ] Tests added or updated for the change
- [ ] `uv run pytest` passes
- [ ] `uv run ruff check hl/ tests/` passes
- [ ] README / docs / `CHANGELOG.md` updated if user-facing
- [ ] No new dependencies unless justified

## Adding a command

### Read-only (Stage 1)

1. Add `cmd_xxx(args)` to `hl/cli.py` calling the appropriate `Info` method.
2. Register the subparser in `build_parser()` with `set_defaults(func=cmd_xxx)`.
3. Add tests under `tests/` using `MagicMock` for the SDK.
4. Add a row to the command table in `README.md`.

### Signed execution (Stage 2)

1. Add `cmd_xxx(args)` to `hl/orders.py`.
2. Load `hl_config.load()`, call `make_info()` for preflight reads, call `make_exchange()` for the signing path.
3. Wrap preflight in `try/except PreflightError → fail("preflight_error", ...)`.
4. Wrap the SDK call in `try/except → fail("execution_error", ...)` and log via `log_attempt(...)`.
5. Support `--preview`: print the signed payload as JSON and return without a network call.
6. Register the subparser in `register_order_subparsers()`.
7. Add tests for the happy path (mocked SDK), validation rejections, and the `--preview` shape.

## Reporting bugs

Open an issue with:

- `hl --help` output
- The exact command you ran
- The full JSON output (stdout and stderr)
- Python version (`python --version`)
- `hyperliquid-cli` version (`pip show hyperliquid-cli`)

For suspected security issues, see [`SECURITY.md`](SECURITY.md) instead.

## License

By contributing, you agree that your contributions will be licensed under the MIT License — see [`LICENSE`](LICENSE).
