"""Read-only Kalshi public market-data client.

Kalshi documents its market data (series, events, markets, trades, order books, candlesticks, historical archive)
as public and readable without credentials: https://docs.kalshi.com/getting_started/quick_start_market_data
This client sends no credentials, only issues GET, and refuses any path outside the allowlist below, so it cannot
reach portfolio or order endpoints.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from meridian.core.logging import get_logger
from meridian.marketdata.coinbase import ForbiddenRequest, TokenBucket
from meridian.marketdata.types import ProviderUnavailable

log = get_logger("meridian.predictions.kalshi")

BASE_URL = "https://api.elections.kalshi.com"
_T = r"[A-Za-z0-9\-\.]+"
ALLOWED_PATHS = tuple(re.compile(p) for p in (
    r"^/trade-api/v2/markets$",
    rf"^/trade-api/v2/markets/{_T}$",
    rf"^/trade-api/v2/markets/{_T}/orderbook$",
    r"^/trade-api/v2/markets/trades$",
    r"^/trade-api/v2/markets/candlesticks$",
    rf"^/trade-api/v2/series/{_T}$",
    rf"^/trade-api/v2/series/{_T}/markets/{_T}/candlesticks$",
    r"^/trade-api/v2/historical/cutoff$",
    r"^/trade-api/v2/historical/markets$",
    rf"^/trade-api/v2/historical/markets/{_T}$",
    rf"^/trade-api/v2/historical/markets/{_T}/candlesticks$",
))
FORBIDDEN_FRAGMENTS = ("/portfolio", "/orders", "/order")  # banned-pattern definition (blocklist: these paths are refused)
API = "/trade-api/v2"


def check_allowed(method: str, path: str) -> None:
    if method.upper() != "GET":
        raise ForbiddenRequest(f"method {method} not permitted: MERIDIAN is read-only")
    if any(f in path for f in FORBIDDEN_FRAGMENTS) or not any(p.match(path) for p in ALLOWED_PATHS):
        raise ForbiddenRequest(f"path {path} is not on the Kalshi public market-data allowlist")


class KalshiPublicClient:
    """GET-only, credential-free. Kalshi's basic tier allows 20 reads/s; we stay well under it."""

    def __init__(self, transport: httpx.BaseTransport | None = None, rate_per_s: float = 8.0, max_retries: int = 2):
        self._http = httpx.Client(base_url=BASE_URL, timeout=10.0, transport=transport,
                                  headers={"Accept": "application/json", "User-Agent": "meridian-readonly/0.1"})
        self._bucket = TokenBucket(rate_per_s, burst=int(rate_per_s))
        self._max_retries = max_retries
        self.calls = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        full = path if path.startswith(API) else API + path
        check_allowed("GET", full)
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._bucket.acquire()
            self.calls += 1
            try:
                r = self._http.get(full, params=params)
            except httpx.HTTPError as e:
                last = e
                time.sleep(min(2 ** attempt, 4))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = ProviderUnavailable(f"HTTP {r.status_code} from {full}")
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 10))
                continue
            if r.status_code != 200:
                raise ProviderUnavailable(f"HTTP {r.status_code} from {full}: {r.text[:200]}")
            return r.json()
        raise ProviderUnavailable(f"kalshi unavailable after retries: {last!r}")


# ------------------------------------------------------------------ parsing (tolerant of the cents -> dollars migration)
def dollars(obj: dict[str, Any] | None, key: str) -> Decimal | None:
    """Read `<key>_dollars` (string dollars) or fall back to the deprecated integer-cents `<key>` field."""
    if not obj:
        return None
    v = obj.get(f"{key}_dollars")
    if v not in (None, ""):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    v = obj.get(key)
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v)) / 100
    except InvalidOperation:
        return None


def num(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def ts(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return datetime.fromtimestamp(int(v), UTC)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


@dataclass
class MarketQuote:
    ticker: str
    event_ticker: str | None
    open_time: datetime | None
    close_time: datetime | None
    strike: Decimal | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    last: Decimal | None
    status: str
    result: str | None  # "yes" | "no" | None
    volume: Decimal | None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_market(m: dict[str, Any]) -> MarketQuote:
    result = str(m.get("result") or m.get("market_result") or "").lower() or None
    if result not in ("yes", "no"):
        result = None
        sv = dollars(m, "settlement_value")
        status = str(m.get("status") or "").lower()
        if sv is not None and status in ("settled", "finalized", "determined"):
            result = "yes" if sv >= Decimal("0.99") else "no" if sv <= Decimal("0.01") else None
    return MarketQuote(
        ticker=m["ticker"], event_ticker=m.get("event_ticker"), open_time=ts(m.get("open_time")),
        close_time=ts(m.get("close_time")), strike=num(m.get("floor_strike")),
        yes_bid=dollars(m, "yes_bid"), yes_ask=dollars(m, "yes_ask"), last=dollars(m, "last_price"),
        status=str(m.get("status") or ""), result=result,
        volume=num(m.get("volume_fp")) if m.get("volume_fp") not in (None, "") else num(m.get("volume")),
        raw={k: m.get(k) for k in ("ticker", "event_ticker", "status", "strike_type", "floor_strike", "cap_strike",
                                   "yes_sub_title", "rules_primary", "open_time", "close_time")},
    )


@dataclass
class Bar:
    minute: int          # 1..15: bar ending `minute` minutes after the window opened
    end_ts: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_high: Decimal | None
    yes_bid_low: Decimal | None
    yes_ask_high: Decimal | None
    yes_ask_low: Decimal | None
    volume: Decimal | None


def _ohlc(c: dict[str, Any], side: str, part: str) -> Decimal | None:
    return dollars(c.get(side) or {}, part)


def parse_candles(candles: list[dict[str, Any]], open_time: datetime) -> list[Bar]:
    out: list[Bar] = []
    for c in candles or []:
        end = ts(c.get("end_period_ts"))
        if end is None:
            continue
        minute = round((end - open_time).total_seconds() / 60)
        if not 1 <= minute <= 15:
            continue
        vol = num(c.get("volume_fp")) if c.get("volume_fp") not in (None, "") else num(c.get("volume"))
        out.append(Bar(minute=minute, end_ts=end,
                       yes_bid=_ohlc(c, "yes_bid", "close"), yes_ask=_ohlc(c, "yes_ask", "close"),
                       yes_bid_high=_ohlc(c, "yes_bid", "high"), yes_bid_low=_ohlc(c, "yes_bid", "low"),
                       yes_ask_high=_ohlc(c, "yes_ask", "high"), yes_ask_low=_ohlc(c, "yes_ask", "low"), volume=vol))
    return sorted(out, key=lambda b: b.minute)


class KalshiMarkets:
    """Higher-level reads used by the desk."""

    def __init__(self, client: KalshiPublicClient | None = None):
        self.client = client or KalshiPublicClient()

    def list_markets(self, series: str, status: str, min_close_ts: int | None = None, max_pages: int = 20,
                     historical: bool = False) -> list[MarketQuote]:
        path = "/historical/markets" if historical else "/markets"
        params: dict[str, Any] = {"series_ticker": series, "limit": 1000}
        if status and not historical:
            params["status"] = status
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        out: list[MarketQuote] = []
        for _ in range(max_pages):
            data = self.client.get(path, params)
            for m in data.get("markets") or []:
                if m.get("ticker"):
                    out.append(parse_market(m))
            cur = data.get("cursor")
            if not cur:
                break
            params["cursor"] = cur
        return out

    def market(self, ticker: str) -> MarketQuote:
        return parse_market(self.client.get(f"/markets/{ticker}")["market"])

    def open_window(self, series: str, now: datetime) -> MarketQuote | None:
        live = [m for m in self.list_markets(series, "open", max_pages=1)
                if m.close_time and m.close_time > now and (m.open_time is None or m.open_time <= now)]
        return min(live, key=lambda m: m.close_time) if live else None  # type: ignore[arg-type, return-value]

    def cutoff(self) -> datetime | None:
        try:
            d = self.client.get("/historical/cutoff")
        except ProviderUnavailable:
            return None
        return ts(d.get("market_settled_ts"))

    def batch_candles(self, windows: list[MarketQuote]) -> dict[str, list[Bar]]:
        if not windows:
            return {}
        start = int(min(w.open_time for w in windows if w.open_time).timestamp())  # type: ignore[union-attr]
        end = int(max(w.close_time for w in windows if w.close_time).timestamp())  # type: ignore[union-attr]
        data = self.client.get("/markets/candlesticks", {"tickers": ",".join(w.ticker for w in windows),
                                                          "start_ts": start, "end_ts": end, "period_interval": 1})
        by = {w.ticker: w for w in windows}
        out: dict[str, list[Bar]] = {}
        for m in data.get("markets") or []:
            t = m.get("market_ticker") or m.get("ticker")
            if t in by and by[t].open_time:
                out[t] = parse_candles(m.get("candlesticks") or [], by[t].open_time)  # type: ignore[arg-type]
        return out

    def candles(self, series: str, w: MarketQuote, historical: bool) -> list[Bar]:
        assert w.open_time and w.close_time
        params = {"start_ts": int(w.open_time.timestamp()), "end_ts": int(w.close_time.timestamp()), "period_interval": 1}
        path = f"/historical/markets/{w.ticker}/candlesticks" if historical else f"/series/{series}/markets/{w.ticker}/candlesticks"
        data = self.client.get(path, params)
        return parse_candles(data.get("candlesticks") or [], w.open_time)
