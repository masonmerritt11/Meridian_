"""Read-only Coinbase account reader (the CIO's own futures account).

Safety, in layers:
  1. The CIO creates a CDP API key with ONLY the "View" permission. Coinbase itself then refuses any order.
  2. On every sync we call GET /key_permissions and stop (fail closed) if the key can trade or transfer.
  3. This client sends only GET, and only to the allowlisted read endpoints below.
The key is read from environment variables and never logged.

Auth: a short-lived JWT per request (CDP API keys). ES256 for PEM EC keys (recommended for Advanced Trade),
EdDSA for base64 Ed25519 keys. uri claim = "GET api.coinbase.com<path>" (no query string).
"""
from __future__ import annotations

import base64
import re
import secrets
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meridian.marketdata.coinbase import ForbiddenRequest, TokenBucket
from meridian.marketdata.types import ProviderUnavailable

HOST = "api.coinbase.com"
ACCOUNT_READ_PATHS = tuple(re.compile(p) for p in (
    r"^/api/v3/brokerage/key_permissions$",
    r"^/api/v3/brokerage/cfm/positions$",
    r"^/api/v3/brokerage/cfm/positions/[A-Za-z0-9\-]+$",
    r"^/api/v3/brokerage/orders/historical/batch$",
    r"^/api/v3/brokerage/orders/historical/fills$",
))


class KeyNotReadOnly(RuntimeError):
    pass


def check_account_path(method: str, path: str) -> None:
    if method.upper() != "GET":
        raise ForbiddenRequest(f"method {method} not permitted: MERIDIAN only reads your account")
    if not any(p.match(path) for p in ACCOUNT_READ_PATHS):
        raise ForbiddenRequest(f"path {path} is not on the read-only account allowlist")


def _signing_key(secret: str) -> tuple[Any, str]:
    s = secret.strip().replace("\\n", "\n")
    if "BEGIN" in s:
        return serialization.load_pem_private_key(s.encode(), password=None), "ES256"
    raw = base64.b64decode(s)
    if len(raw) not in (32, 64):
        raise ValueError("unrecognised API key secret format (expected an EC PEM key or a base64 Ed25519 key)")
    return Ed25519PrivateKey.from_private_bytes(raw[:32]), "EdDSA"


def build_jwt(key_name: str, secret: str, method: str, path: str, now: int | None = None) -> str:
    key, alg = _signing_key(secret)
    now = int(now or time.time())
    payload = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"{method.upper()} {HOST}{path}"}
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": key_name, "nonce": secrets.token_hex(16), "typ": "JWT"})


class CoinbaseAccountReader:
    def __init__(self, key_name: str, secret: str, transport: httpx.BaseTransport | None = None):
        if not key_name or not secret:
            raise ValueError("key name and secret are required")
        _signing_key(secret)  # fail early on a malformed secret (without echoing it)
        self._key_name, self._secret = key_name, secret
        self._http = httpx.Client(base_url=f"https://{HOST}", timeout=15.0, transport=transport,
                                  headers={"Accept": "application/json", "User-Agent": "meridian-readonly/0.1"})
        self._bucket = TokenBucket(5, burst=5)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        check_account_path("GET", path)
        self._bucket.acquire()
        token = build_jwt(self._key_name, self._secret, "GET", path)
        r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise ProviderUnavailable(f"Coinbase rejected the API key (401). {self.describe()}. Coinbase said: {r.text[:160]!r}")
        if r.status_code != 200:
            raise ProviderUnavailable(f"HTTP {r.status_code} from {path}: {r.text[:200]}")
        return r.json()

    def describe(self) -> str:
        """Safe diagnostics: key type and name shape only, never the secret."""
        _, alg = _signing_key(self._secret)
        kind = "ECDSA key (correct type)" if alg == "ES256" else "Ed25519 key (Coinbase's trading API needs an ECDSA key)"
        if self._key_name.startswith("organizations/") and "/apiKeys/" in self._key_name:
            shape = "name format organizations/…/apiKeys/… (correct)"
        elif re.fullmatch(r"[0-9a-fA-F\-]{30,40}", self._key_name):
            shape = "name is a bare key ID (try the full organizations/…/apiKeys/… name)"
        else:
            shape = "name format not recognised (expected organizations/…/apiKeys/…)"
        return f"MERIDIAN sees an {kind}; {shape}"

    def assert_read_only(self) -> dict[str, Any]:
        perms = self.get("/api/v3/brokerage/key_permissions")
        if perms.get("can_trade") or perms.get("can_transfer"):
            raise KeyNotReadOnly("this API key can trade or transfer; MERIDIAN only accepts a View-only key")
        if not perms.get("can_view", True):
            raise KeyNotReadOnly("this API key cannot view the account")
        return perms

    def positions(self) -> list[dict[str, Any]]:
        return self.get("/api/v3/brokerage/cfm/positions").get("positions") or []

    def open_orders(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"order_status": "OPEN", "product_type": "FUTURE", "limit": 250}
        for _ in range(5):
            d = self.get("/api/v3/brokerage/orders/historical/batch", params)
            out += d.get("orders") or []
            if not d.get("has_next") or not d.get("cursor"):
                break
            params["cursor"] = d["cursor"]
        return out

    def fills(self, start_iso: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"start_sequence_timestamp": start_iso, "limit": 250}
        for _ in range(20):
            d = self.get("/api/v3/brokerage/orders/historical/fills", params)
            out += d.get("fills") or []
            if not d.get("cursor"):
                break
            params["cursor"] = d["cursor"]
        return out
