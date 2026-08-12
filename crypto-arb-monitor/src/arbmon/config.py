"""Configuration loading, validation, and the API-key safety gate.

The safety gate is the one piece of security-relevant logic here. arbmon never
trades, so it never *needs* keys. But the spec requires that IF keys are ever
added, the system must refuse any key that can withdraw funds. We enforce that
with a READ-ONLY call to Binance's apiRestrictions endpoint and fail closed:
if withdrawals are enabled, or we cannot verify, we refuse to start.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


class WithdrawalPermissionError(ConfigError):
    """Raised when a supplied API key can withdraw funds (or can't be verified)."""


@dataclass
class Config:
    raw: dict[str, Any]

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path lookup, e.g. cfg.get('venues.binance.taker_fee')."""
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: str) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy config.example.yaml to "
            f"config.yaml and edit it."
        )
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    cfg = Config(raw=raw)
    _validate(cfg)
    _enforce_key_safety(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    fee_b = cfg.get("venues.binance.taker_fee")
    fee_c = cfg.get("venues.coinbase.taker_fee")
    for name, fee in (("binance", fee_b), ("coinbase", fee_c)):
        if fee is None or not (0.0 <= float(fee) < 1.0):
            raise ConfigError(f"venues.{name}.taker_fee must be in [0, 1)")
    if not cfg.get("venues.binance.symbols"):
        raise ConfigError("venues.binance.symbols must list at least one symbol")
    if float(cfg.get("execution.latency_ms", 0)) < 0:
        raise ConfigError("execution.latency_ms must be >= 0")
    if float(cfg.get("paper.starting_notional_quote", 0)) <= 0:
        raise ConfigError("paper.starting_notional_quote must be > 0")


# ---------------------------------------------------------------------------
# API-key safety gate (read-only; never signs an order)
# ---------------------------------------------------------------------------

def _enforce_key_safety(cfg: Config) -> None:
    keys = cfg.get("keys")
    if not keys:
        return  # No keys configured — the safe, default state.
    binance_keys = keys.get("binance") if isinstance(keys, dict) else None
    if not binance_keys:
        return
    api_key = binance_keys.get("api_key")
    api_secret = binance_keys.get("api_secret")
    require_no_withdrawal = bool(binance_keys.get("require_no_withdrawal", True))
    if not (api_key and api_secret):
        return
    if not require_no_withdrawal:
        raise WithdrawalPermissionError(
            "keys.binance.require_no_withdrawal must be true; arbmon refuses to "
            "run with an unchecked key."
        )
    restrictions = _fetch_binance_api_restrictions(api_key, api_secret)
    if restrictions.get("enableWithdrawals", True):
        raise WithdrawalPermissionError(
            "Refusing to start: the supplied Binance API key has WITHDRAWALS "
            "ENABLED. Create a key with trading/reading only (no withdrawal) "
            "and try again."
        )
    log.warning(
        "API key present and verified withdrawal-disabled. Note: arbmon still "
        "never places orders — keys are used only for this safety check."
    )


def _fetch_binance_api_restrictions(api_key: str, api_secret: str) -> dict:
    """READ-ONLY GET /sapi/v1/account/apiRestrictions. Returns the parsed JSON.

    Fails closed: any error verifying the key raises WithdrawalPermissionError,
    so an unverifiable key is treated as unsafe. This function does not, and
    must not, ever place, cancel, or sign an order — it only reads permissions.
    """
    import json as _json

    base = "https://api.binance.com"
    endpoint = "/sapi/v1/account/apiRestrictions"
    query = urllib.parse.urlencode({
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    })
    signature = hmac.new(
        api_secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    url = f"{base}{endpoint}?{query}&signature={signature}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - fail closed on any error
        raise WithdrawalPermissionError(
            f"Could not verify API key permissions (failing closed): {exc}"
        ) from exc
