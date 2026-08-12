"""Tests for config validation and the API-key withdrawal safety gate.

We do NOT hit the network here. We verify: (1) the default/no-key path is
allowed, (2) an unchecked key is refused, and (3) when a key IS present the
verified-restrictions path is consulted and a withdrawal-enabled key is refused.
"""
import textwrap

import pytest

import arbmon.config as cfgmod
from arbmon.config import (
    ConfigError,
    WithdrawalPermissionError,
    load_config,
)

_BASE = """
run: {duration_seconds: 1, kill_switch_file: KILL}
storage: {sqlite_path: data/x.sqlite}
venues:
  binance: {ws_base: wss://x, taker_fee: 0.001, symbols: [BTCUSDT]}
  coinbase: {ws_url: wss://y, taker_fee: 0.006, products: [BTC-USDT]}
execution: {latency_ms: 250, order_notional_quote: 1000}
paper: {starting_notional_quote: 100000}
risk: {max_position_per_pair_quote: 5000, max_daily_sim_loss_quote: 1000}
"""


def _write(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(_BASE) + textwrap.dedent(extra))
    return str(p)


def test_no_keys_is_allowed(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.get("venues.binance.taker_fee") == 0.001


def test_bad_fee_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(_BASE).replace("taker_fee: 0.001", "taker_fee: 1.5"))
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_unchecked_key_is_refused(tmp_path):
    path = _write(tmp_path, """
        keys:
          binance: {api_key: abc, api_secret: def, require_no_withdrawal: false}
    """)
    with pytest.raises(WithdrawalPermissionError):
        load_config(path)


def test_withdrawal_enabled_key_refused(tmp_path, monkeypatch):
    # Stub the read-only permission check to report withdrawals ENABLED.
    monkeypatch.setattr(
        cfgmod, "_fetch_binance_api_restrictions",
        lambda k, s: {"enableWithdrawals": True})
    path = _write(tmp_path, """
        keys:
          binance: {api_key: abc, api_secret: def, require_no_withdrawal: true}
    """)
    with pytest.raises(WithdrawalPermissionError):
        load_config(path)


def test_withdrawal_disabled_key_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cfgmod, "_fetch_binance_api_restrictions",
        lambda k, s: {"enableWithdrawals": False, "enableReading": True})
    path = _write(tmp_path, """
        keys:
          binance: {api_key: abc, api_secret: def, require_no_withdrawal: true}
    """)
    cfg = load_config(path)   # must not raise
    assert cfg.get("keys.binance.api_key") == "abc"


def test_missing_config_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/path/config.yaml")
