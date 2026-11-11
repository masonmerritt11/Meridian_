"""SIMULATED market data. Deterministic, seeded, and labeled SIMULATED at every layer (instrument ids are
prefixed SIM-, every row carries source=SIMULATED, the UI shows a banner). Contract specs below are
illustrative round numbers, NOT exchange specifications. Never presented as live market information."""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from meridian.marketdata.types import GRANULARITY_SECONDS, BookDTO, CandleDTO, InstrumentSpec

ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)

# asset: (start_price, hourly_vol, tick, contract_size, size_increment)
SIM_SPECS: dict[str, tuple[float, float, str, str, str]] = {
    "GOLD": (4000.0, 0.0035, "0.5", "1", "1"),
    "SILVER": (48.0, 0.0060, "0.005", "10", "1"),
    "OIL": (62.0, 0.0070, "0.01", "10", "1"),
    "BTC": (95000.0, 0.0080, "5", "0.01", "1"),
    "ETH": (3200.0, 0.0100, "0.5", "0.1", "1"),
    "XRP": (2.2, 0.0120, "0.0001", "500", "1"),
}

# Demo scenario overlays (only when scenario="demo"): bars of drift ending at the latest closed bar.
DEMO_OVERLAYS: dict[str, float] = {"GOLD": +1.0, "OIL": -1.0}


def _hours_since_anchor(ts: datetime) -> int:
    return int((ts - ANCHOR).total_seconds() // 3600)


_PATHS: dict[str, tuple[list[float], random.Random, list[float]]] = {}


def _close(asset: str, h: int) -> float:
    """Deterministic hourly close path with alternating trend / range regimes (extends lazily)."""
    if asset not in _PATHS:
        _PATHS[asset] = ([SIM_SPECS[asset][0]], random.Random(f"meridian-sim-{asset}"), [0.0, 0.0])
    prices, rng, state = _PATHS[asset]
    vol = SIM_SPECS[asset][1]
    while len(prices) <= h:
        if state[1] <= 0:
            state[1] = rng.randint(48, 200)
            state[0] = rng.choice([-1, 0, 0, 1]) * vol * rng.uniform(0.15, 0.35)
        state[1] -= 1
        pull = -0.002 * math.log(prices[-1] / SIM_SPECS[asset][0])  # mild mean reversion keeps levels realistic
        prices.append(prices[-1] * math.exp(state[0] + pull + vol * rng.gauss(0, 1)))
    return prices[max(h, 0)]


def _q(x: float, tick: Decimal) -> Decimal:
    return (Decimal(str(x)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * tick


class SimulatedProvider:
    source = "SIMULATED"

    def __init__(self, scenario: str | None = None, now: datetime | None = None, demo_end: datetime | None = None):
        self.scenario = scenario
        self._now = now
        self.demo_end = demo_end  # bar at which the demo breakout completes; offset persists afterwards

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def discover(self) -> list[InstrumentSpec]:
        out = []
        for asset, (_, _, tick, csize, sinc) in SIM_SPECS.items():
            out.append(InstrumentSpec(
                id=f"SIM-{asset}", asset=asset, venue="SIMULATED", product_type="FUTURE",
                display_name=f"{asset} (SIMULATED contract — illustrative specs)", quote_currency="USD",
                price_increment=Decimal(tick), size_increment=Decimal(sinc), min_size=Decimal(sinc),
                contract_size=Decimal(csize), contract_expiry=self.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=45),
                source="SIMULATED", status="ACTIVE", status_detail="SIMULATED DATA — not market information",
            ))
        return out

    def _close_at(self, asset: str, h: int, last_h: int) -> float:
        base = _close(asset, h)
        if self.scenario == "demo" and asset in DEMO_OVERLAYS:
            span = 80
            k = min(h, last_h) - (last_h - span)
            if k > 0:
                vol = SIM_SPECS[asset][1]
                base *= math.exp(DEMO_OVERLAYS[asset] * vol * 0.9 * k)
        return base

    def candles(self, inst: InstrumentSpec, granularity: str, start: datetime, end: datetime) -> list[CandleDTO]:
        if granularity != "ONE_HOUR":
            raise ValueError("simulated provider generates ONE_HOUR candles only")
        tick = inst.price_increment or Decimal("0.01")
        vol = SIM_SPECS[inst.asset][1]
        step = GRANULARITY_SECONDS[granularity]
        last_closed = datetime.fromtimestamp((int(self.now().timestamp()) // step) * step, UTC) - timedelta(seconds=step)
        last_h = _hours_since_anchor(self.demo_end or last_closed)
        t = datetime.fromtimestamp((int(start.timestamp()) // step) * step, UTC)
        out: list[CandleDTO] = []
        while t <= min(end, last_closed):
            h = _hours_since_anchor(t)
            o = self._close_at(inst.asset, h - 1, last_h)
            c = self._close_at(inst.asset, h, last_h)
            rng = random.Random(f"{inst.asset}-{h}")
            hi = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.4)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.4)))
            qo, qc, qh, ql = _q(o, tick), _q(c, tick), _q(hi, tick), _q(lo, tick)
            qh, ql = max(qh, qo, qc), min(ql, qo, qc)
            out.append(CandleDTO(ts=t, open=qo, high=qh, low=ql, close=qc,
                                 volume=Decimal(str(round(rng.uniform(200, 2000), 2)))))
            t += timedelta(seconds=step)
        return out

    def book(self, inst: InstrumentSpec) -> BookDTO | None:
        cs = self.candles(inst, "ONE_HOUR", self.now() - timedelta(hours=2), self.now())
        if not cs:
            return None
        tick = inst.price_increment or Decimal("0.01")
        mid = cs[-1].close
        return BookDTO(ts=self.now(), best_bid=mid - tick, best_ask=mid + tick, bid_depth=Decimal(50), ask_depth=Decimal(50))
