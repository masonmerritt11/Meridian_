from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

GRANULARITY_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600, "TWO_HOUR": 7200, "FOUR_HOUR": 14400, "SIX_HOUR": 21600, "ONE_DAY": 86400,
}

Source = Literal["SIMULATED", "COINBASE"]


@dataclass(frozen=True)
class AssetDef:
    asset: str
    name: str
    asset_class: Literal["METAL", "ENERGY", "CRYPTO"]
    correlation_group: str
    aliases: tuple[str, ...]
    prefer: tuple[str, ...]  # ordered preference: PERPETUAL, EXPIRING, SPOT
    spot_product: str | None = None


UNIVERSE: tuple[AssetDef, ...] = (
    AssetDef("GOLD", "Gold", "METAL", "metals", ("GOLD", "XAU", "GOL"), ("EXPIRING",)),
    AssetDef("SILVER", "Silver", "METAL", "metals", ("SILVER", "XAG", "SLR", "SLV"), ("EXPIRING",)),
    AssetDef("OIL", "Crude Oil", "ENERGY", "energy", ("OIL", "CRUDE", "WTI", "CL", "CRUDE OIL"), ("EXPIRING",)),
    AssetDef("BTC", "Bitcoin", "CRYPTO", "crypto", ("BTC", "BITCOIN"), ("PERPETUAL", "EXPIRING", "SPOT"), "BTC-USD"),
    AssetDef("ETH", "Ethereum", "CRYPTO", "crypto", ("ETH", "ETHER", "ETHEREUM"), ("PERPETUAL", "EXPIRING", "SPOT"), "ETH-USD"),
    AssetDef("XRP", "XRP", "CRYPTO", "crypto", ("XRP",), ("PERPETUAL", "EXPIRING", "SPOT"), "XRP-USD"),
)
ASSETS: dict[str, AssetDef] = {a.asset: a for a in UNIVERSE}


@dataclass
class InstrumentSpec:
    id: str
    asset: str
    venue: str
    product_type: Literal["FUTURE", "SPOT"]
    display_name: str
    quote_currency: str
    price_increment: Decimal | None
    size_increment: Decimal | None
    min_size: Decimal | None
    contract_size: Decimal | None
    contract_expiry: datetime | None
    source: Source
    status: Literal["ACTIVE", "UNAVAILABLE", "EXPIRED"] = "ACTIVE"
    status_detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandleDTO:
    ts: datetime  # bucket start, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class BookDTO:
    ts: datetime
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_depth: Decimal | None
    ask_depth: Decimal | None

    @property
    def spread_bps(self) -> Decimal | None:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            mid = (self.best_bid + self.best_ask) / 2
            return ((self.best_ask - self.best_bid) / mid * Decimal(10_000)).quantize(Decimal("0.01"))
        return None


class ProviderUnavailable(Exception):
    """Raised when a provider cannot serve a request. Callers must record an UNAVAILABLE state."""


class MarketDataProvider(Protocol):
    source: Source

    def discover(self) -> list[InstrumentSpec]: ...

    def candles(self, inst: InstrumentSpec, granularity: str, start: datetime, end: datetime) -> list[CandleDTO]: ...

    def book(self, inst: InstrumentSpec) -> BookDTO | None: ...


class HistoricalDataProvider(Protocol):
    """Interface for additional historical providers (e.g. CME settlement archives). Not implemented in M1."""

    name: str

    def history(self, asset: str, granularity: str, start: datetime, end: datetime) -> list[CandleDTO]: ...


def margin_rate(raw: dict[str, Any] | None, direction: str) -> Decimal | None:
    """Exchange-published margin rate for a futures product (fraction of notional), using the higher of
    the intraday and overnight rates so positions held overnight are never under-reserved."""
    fpd = (raw or {}).get("future_product_details") or {}
    key = "long_margin_rate" if direction == "LONG" else "short_margin_rate"
    rates = []
    for k in ("intraday_margin_rate", "overnight_margin_rate"):
        v = (fpd.get(k) or {}).get(key) if isinstance(fpd.get(k), dict) else None
        try:
            if v not in (None, "") and Decimal(str(v)) > 0:
                rates.append(Decimal(str(v)))
        except Exception:  # noqa: BLE001
            continue
    return max(rates) if rates else None
