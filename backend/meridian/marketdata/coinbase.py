"""Read-only Coinbase Advanced Trade *public* market-data adapter.

Uses only unauthenticated GET endpoints under /api/v3/brokerage/market/* (plus /time), documented at
https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-overview. No credentials are accepted or sent.
The HTTP layer enforces a method + path allowlist so this module physically cannot reach order endpoints.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from meridian.core.logging import get_logger
from meridian.marketdata.types import (
    ASSETS,
    GRANULARITY_SECONDS,
    UNIVERSE,
    AssetDef,
    BookDTO,
    CandleDTO,
    InstrumentSpec,
    ProviderUnavailable,
)

log = get_logger("meridian.marketdata.coinbase")

BASE_URL = "https://api.coinbase.com"
ALLOWED_PATHS = (
    re.compile(r"^/api/v3/brokerage/market/products$"),
    re.compile(r"^/api/v3/brokerage/market/products/[A-Za-z0-9\-]+$"),
    re.compile(r"^/api/v3/brokerage/market/products/[A-Za-z0-9\-]+/candles$"),
    re.compile(r"^/api/v3/brokerage/market/product_book$"),
    re.compile(r"^/api/v3/brokerage/time$"),
)
MAX_CANDLES_PER_REQUEST = 350  # documented maximum


class ForbiddenRequest(Exception):
    pass


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.capacity, self.tokens = rate_per_s, burst, float(burst)
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class ReadOnlyPublicClient:
    """GET-only HTTP client restricted to the public market-data allowlist."""

    def __init__(self, transport: httpx.BaseTransport | None = None, rate_per_s: float = 5.0, max_retries: int = 3):
        self._http = httpx.Client(base_url=BASE_URL, timeout=15.0, transport=transport,
                                  headers={"Accept": "application/json", "User-Agent": "meridian-readonly/0.1"})
        self._bucket = TokenBucket(rate_per_s, burst=int(rate_per_s))
        self._max_retries = max_retries
        self.calls = 0
        self.rate_limited = 0

    @staticmethod
    def check_allowed(method: str, path: str) -> None:
        if method.upper() != "GET":
            raise ForbiddenRequest(f"method {method} not permitted: MERIDIAN is read-only")
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise ForbiddenRequest(f"path {path} is not on the public market-data allowlist")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.check_allowed("GET", path)
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._bucket.acquire()
            self.calls += 1
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code == 429:
                self.rate_limited += 1
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("coinbase rate limited", extra={"path": path, "retry_after": retry_after})
                time.sleep(min(retry_after, 30))
                continue
            if resp.status_code >= 500:
                last_err = ProviderUnavailable(f"{resp.status_code} from {path}")
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code != 200:
                raise ProviderUnavailable(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
            return resp.json()
        raise ProviderUnavailable(f"coinbase unavailable after retries: {last_err!r}")


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _matches(asset: AssetDef, product: dict[str, Any]) -> bool:
    fpd = product.get("future_product_details") or {}
    root = str(fpd.get("contract_root_unit") or "").upper()
    text = " ".join(str(x or "") for x in (fpd.get("group_description"), fpd.get("contract_display_name"),
                                           fpd.get("group_short_description"), product.get("display_name"))).upper()
    if root in asset.aliases:
        return True
    return any(re.search(rf"\b{re.escape(a)}\b", text) for a in asset.aliases if len(a) >= 3)


def product_to_spec(asset: str, p: dict[str, Any]) -> InstrumentSpec:
    fpd = p.get("future_product_details") or {}
    is_future = str(p.get("product_type", "")).upper() == "FUTURE"
    return InstrumentSpec(
        id=p["product_id"], asset=asset, venue=str(fpd.get("venue") or ("CDE" if is_future else "COINBASE")),
        product_type="FUTURE" if is_future else "SPOT",
        display_name=str(fpd.get("contract_display_name") or p.get("display_name") or p["product_id"]),
        quote_currency=str(p.get("quote_currency_id") or "USD"),
        price_increment=_dec(p.get("price_increment") or p.get("quote_increment")),
        size_increment=_dec(p.get("base_increment")), min_size=_dec(p.get("base_min_size")),
        contract_size=_dec(fpd.get("contract_size")) if is_future else Decimal(1),
        contract_expiry=_dt(fpd.get("contract_expiry")), source="COINBASE",
        raw={k: p.get(k) for k in ("product_id", "product_type", "status", "trading_disabled", "is_disabled",
                                   "price_increment", "base_increment", "base_min_size", "quote_currency_id")}
        | {"future_product_details": {k: fpd.get(k) for k in ("venue", "contract_code", "contract_expiry", "contract_size",
                                                              "contract_root_unit", "group_description",
                                                              "contract_expiry_type", "contract_display_name",
                                                              "intraday_margin_rate", "overnight_margin_rate",
                                                              "index_price", "settlement_price")}},
    )


def select_product(asset: AssetDef, products: list[dict[str, Any]], now: datetime, min_days: int = 3) -> dict[str, Any] | None:
    live = [p for p in products if not p.get("trading_disabled") and not p.get("is_disabled")
            and str(p.get("status", "online")).lower() in ("online", "")]
    for kind in asset.prefer:
        if kind == "SPOT":
            spot = [p for p in live if p.get("product_id") == asset.spot_product]
            if spot:
                return spot[0]
            continue
        cands = []
        for p in live:
            if str(p.get("product_type", "")).upper() != "FUTURE" or not _matches(asset, p):
                continue
            fpd = p.get("future_product_details") or {}
            etype = str(fpd.get("contract_expiry_type", "")).upper()
            exp = _dt(fpd.get("contract_expiry"))
            if kind == "PERPETUAL" and etype != "PERPETUAL":
                continue
            if kind == "EXPIRING" and etype == "PERPETUAL":
                continue
            if exp is not None and exp - now < timedelta(days=min_days):
                continue
            cands.append((exp or datetime.max.replace(tzinfo=UTC), p))
        if cands:
            return sorted(cands, key=lambda t: t[0])[0][1]
    return None


class CoinbasePublicProvider:
    source = "COINBASE"

    def __init__(self, client: ReadOnlyPublicClient | None = None, overrides: dict[str, str] | None = None):
        self.client = client or ReadOnlyPublicClient()
        self.overrides = overrides or {}

    def _list(self, product_type: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"product_type": product_type, "limit": 1000}
        for _ in range(20):
            data = self.client.get("/api/v3/brokerage/market/products", params)
            out.extend(data.get("products") or [])
            pag = data.get("pagination") or {}
            if pag.get("has_next") and pag.get("next_cursor"):
                params["cursor"] = pag["next_cursor"]
            else:
                break
        return out

    def discover(self) -> list[InstrumentSpec]:
        now = datetime.now(UTC)
        products = self._list("FUTURE") + self._list("SPOT")
        by_id = {p.get("product_id"): p for p in products}
        specs: list[InstrumentSpec] = []
        for asset in UNIVERSE:
            if asset.asset in self.overrides:
                p = by_id.get(self.overrides[asset.asset])
                if p is None:
                    p = self.client.get(f"/api/v3/brokerage/market/products/{self.overrides[asset.asset]}")
            else:
                p = select_product(asset, products, now)
            if p is None:
                specs.append(InstrumentSpec(id=f"UNRESOLVED-{asset.asset}", asset=asset.asset, venue="COINBASE",
                                            product_type="FUTURE", display_name=asset.name, quote_currency="USD",
                                            price_increment=None, size_increment=None, min_size=None, contract_size=None,
                                            contract_expiry=None, source="COINBASE", status="UNAVAILABLE",
                                            status_detail="No matching tradable Coinbase product found. Set MERIDIAN_PRODUCT_OVERRIDES."))
                continue
            specs.append(product_to_spec(asset.asset, p))
        return specs

    def candles(self, inst: InstrumentSpec, granularity: str, start: datetime, end: datetime) -> list[CandleDTO]:
        step = GRANULARITY_SECONDS[granularity]
        out: list[CandleDTO] = []
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(seconds=step * MAX_CANDLES_PER_REQUEST))
            data = self.client.get(f"/api/v3/brokerage/market/products/{inst.id}/candles",
                                   {"start": str(int(cursor.timestamp())), "end": str(int(chunk_end.timestamp())),
                                    "granularity": granularity, "limit": MAX_CANDLES_PER_REQUEST})
            for c in data.get("candles") or []:
                try:
                    out.append(CandleDTO(ts=datetime.fromtimestamp(int(c["start"]), UTC), open=Decimal(c["open"]),
                                         high=Decimal(c["high"]), low=Decimal(c["low"]), close=Decimal(c["close"]),
                                         volume=Decimal(c["volume"])))
                except (KeyError, ValueError, InvalidOperation):
                    log.warning("malformed candle skipped", extra={"instrument": inst.id, "raw": c})
            cursor = chunk_end
        return out

    def book(self, inst: InstrumentSpec) -> BookDTO | None:
        data = self.client.get("/api/v3/brokerage/market/product_book", {"product_id": inst.id, "limit": 25})
        pb = data.get("pricebook") or {}
        bids, asks = pb.get("bids") or [], pb.get("asks") or []
        if not bids or not asks:
            return None
        return BookDTO(ts=_dt(pb.get("time")) or datetime.now(UTC),
                       best_bid=_dec(bids[0].get("price")), best_ask=_dec(asks[0].get("price")),
                       bid_depth=sum((_dec(b.get("size")) or Decimal(0)) for b in bids),
                       ask_depth=sum((_dec(a.get("size")) or Decimal(0)) for a in asks))


__all__ = ["CoinbasePublicProvider", "ReadOnlyPublicClient", "ForbiddenRequest", "ASSETS"]
