"""Tests for hl/auth.py — env-var loading + main-key derivation guard.

Uses a fresh random secret each run for the main-key guard test so we
never bake a real key into tests.
"""

from __future__ import annotations

import json
import secrets

import pytest
from eth_account import Account

from hl.auth import _load_account_address, _load_secret_key, load_wallet


def _random_secret_and_address() -> tuple[str, str]:
    raw = "0x" + secrets.token_hex(32)
    addr = Account.from_key(raw).address
    return raw, addr


class TestAddressLoader:
    def test_valid_address(self, monkeypatch):
        monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "a" * 40)
        assert _load_account_address() == "0x" + "a" * 40

    def test_missing_address_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
        with pytest.raises(SystemExit):
            _load_account_address()
        out = capsys.readouterr().out
        assert "HL_ACCOUNT_ADDRESS not set" in out

    def test_malformed_address_fails(self, monkeypatch, capsys):
        monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "not-an-address")
        with pytest.raises(SystemExit):
            _load_account_address()
        out = capsys.readouterr().out
        assert "invalid EVM address" in out


class TestSecretKeyLoader:
    def test_valid_key(self, monkeypatch):
        key = "0x" + "1" * 64
        monkeypatch.setenv("HL_SECRET_KEY", key)
        assert _load_secret_key() == key

    def test_missing_key_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("HL_SECRET_KEY", raising=False)
        with pytest.raises(SystemExit):
            _load_secret_key()

    def test_wrong_length_key_fails(self, monkeypatch, capsys):
        monkeypatch.setenv("HL_SECRET_KEY", "0xshortkey")
        with pytest.raises(SystemExit):
            _load_secret_key()


class TestMainKeyGuard:
    def test_api_wallet_derives_to_different_address_passes(self, monkeypatch):
        # Main wallet address (any valid address)
        main_addr = "0x" + "a" * 40
        # API wallet: random key, will derive to a DIFFERENT address
        api_secret, _api_addr = _random_secret_and_address()
        monkeypatch.setenv("HL_ACCOUNT_ADDRESS", main_addr)
        monkeypatch.setenv("HL_SECRET_KEY", api_secret)
        wallet = load_wallet()
        assert wallet.address.lower() != main_addr.lower()

    def test_secret_matching_main_address_blocks(self, monkeypatch, capsys):
        # Generate a key, then set HL_ACCOUNT_ADDRESS to the address that key derives to.
        # This simulates "user pasted the main wallet key into HL_SECRET_KEY".
        secret, addr = _random_secret_and_address()
        monkeypatch.setenv("HL_ACCOUNT_ADDRESS", addr)
        monkeypatch.setenv("HL_SECRET_KEY", secret)
        with pytest.raises(SystemExit):
            load_wallet()
        out = capsys.readouterr().out
        # Envelope is JSON-on-stdout
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["ok"] is False
        assert payload["error"] == "auth"
        assert "MAIN wallet key" in payload["message"]
