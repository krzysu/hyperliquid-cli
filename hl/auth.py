"""Authenticated Exchange client for Hyperliquid Stage 2 execution.

Two env vars are required:
  HL_ACCOUNT_ADDRESS — the main wallet's public address (holds the funds)
  HL_SECRET_KEY      — the API wallet's private key (cannot withdraw)

Safety: if HL_SECRET_KEY derives to the same address as HL_ACCOUNT_ADDRESS,
refuse. That means the user accidentally pasted their main wallet key into
the API-wallet slot, which would expose withdraw authority to this CLI.

Generate the API wallet at https://app.hyperliquid.xyz/API and approve it
from the main wallet (one-time, gas-free).
"""

from __future__ import annotations

import os

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .envelope import fail


def _load_account_address() -> str:
    addr = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
    if not addr:
        fail("config", "HL_ACCOUNT_ADDRESS not set (main wallet public address)")
    if not addr.startswith("0x") or len(addr) != 42:
        fail("validation", f"invalid EVM address: {addr!r}")
    return addr


def _load_secret_key() -> str:
    key = os.environ.get("HL_SECRET_KEY", "").strip()
    if not key:
        fail(
            "config",
            "HL_SECRET_KEY not set — generate an API wallet at https://app.hyperliquid.xyz/API "
            "and approve it from your main wallet before running execution commands",
        )
    if not key.startswith("0x") or len(key) != 66:
        fail("validation", f"HL_SECRET_KEY should be a 0x-prefixed 32-byte hex string, got len={len(key)}")
    return key


def load_wallet() -> LocalAccount:
    """Load the API wallet signer. Enforces the main-key guard."""
    account_address = _load_account_address()
    secret_key = _load_secret_key()

    try:
        wallet = Account.from_key(secret_key)
    except Exception as exc:  # noqa: BLE001
        fail("validation", f"could not derive wallet from HL_SECRET_KEY: {exc}")

    if wallet.address.lower() == account_address.lower():
        fail(
            "auth",
            "HL_SECRET_KEY derives to the same address as HL_ACCOUNT_ADDRESS. "
            "That means you pasted your MAIN wallet key, which has withdraw rights. "
            "This CLI refuses to run with the main key. Create an API wallet instead: "
            "https://app.hyperliquid.xyz/API",
        )
    return wallet


def make_exchange() -> tuple[Exchange, str]:
    """Build an authenticated Exchange client. Returns (exchange, account_address)."""
    wallet = load_wallet()
    account_address = _load_account_address()
    exchange = Exchange(
        wallet=wallet,
        base_url=constants.MAINNET_API_URL,
        account_address=account_address,
    )
    return exchange, account_address


def make_info() -> Info:
    """Build a read-only Info client for preflight checks."""
    return Info(constants.MAINNET_API_URL, skip_ws=True)
