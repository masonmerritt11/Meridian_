"""Macro calendar providers.

FairEconomyWeeklyProvider reads the free public weekly economic-calendar JSON published by Fair Economy
(the ForexFactory feed). It is unofficial, covers the current week only, and does not carry released
'actual' values. Failures produce an explicit UNAVAILABLE state; events are never invented."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

FAIR_ECONOMY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


@dataclass(frozen=True)
class MacroEventDTO:
    id: str
    title: str
    country: str
    impact: str
    event_time: datetime | None
    time_confidence: str
    forecast: str | None
    previous: str | None
    source: str


class MacroCalendarProvider(Protocol):
    name: str

    def fetch(self) -> list[MacroEventDTO]: ...


def parse_fair_economy(items: list[dict[str, Any]], source: str = "fair_economy_weekly") -> list[MacroEventDTO]:
    out = []
    for it in items:
        title, country, raw = str(it.get("title", "")).strip(), str(it.get("country", "")).strip(), it.get("date")
        if not title or not raw:
            continue
        t: datetime | None
        conf = "EXACT"
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if t.tzinfo is None:
                conf = "UNKNOWN"  # never guess a timezone
                t = None
            else:
                t = t.astimezone(UTC)
                if t.hour == 0 and t.minute == 0 and str(it.get("time", "")).lower() in ("all day", "tentative", ""):
                    conf = "DATE_ONLY" if "T" not in str(raw) else conf
        except ValueError:
            t, conf = None, "UNKNOWN"
        eid = hashlib.sha256(f"{source}|{title}|{country}|{raw}".encode()).hexdigest()[:32]
        out.append(MacroEventDTO(id=eid, title=title, country=country, impact=str(it.get("impact", "")).strip() or "Unknown",
                                 event_time=t, time_confidence=conf, forecast=(it.get("forecast") or None),
                                 previous=(it.get("previous") or None), source=source))
    return out


class FairEconomyWeeklyProvider:
    name = "fair_economy_weekly"

    def __init__(self, url: str = FAIR_ECONOMY_URL, transport: httpx.BaseTransport | None = None):
        self.url = url
        self._http = httpx.Client(timeout=15.0, transport=transport, headers={"User-Agent": "meridian-readonly/0.1"})

    def fetch(self) -> list[MacroEventDTO]:
        r = self._http.get(self.url)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("unexpected calendar payload shape")
        return parse_fair_economy(data, self.name)
