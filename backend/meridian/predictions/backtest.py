"""Strategy lab: search a grid of entry/exit rules over recorded 15-minute windows, honestly.

Fill model (conservative): buy at the ask at the close of the entry minute and pay the taker fee. A take-profit is
a resting sell, counted as filled only if the bid traded at least 1¢ THROUGH the target during a later minute
(touching it is not enough: we might be behind others in the queue). Maker orders pay no fee on standard series.
Otherwise sell at the bid at the close of the exit minute and pay the taker fee. Every rule exits by minute 13,
so no strategy ever holds through close.

Overfitting control: windows are split by time. Rules are ranked on the older 70% and only the top few are scored on
the newer 30% they never saw. A rule is PROVEN only if it also makes money there with enough trades to be credible.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from meridian.predictions.model import Sides, edge, fair_yes, fee_per_contract

LAST_EXIT_MINUTE = 13  # exit no later than 2 minutes before close
TP_TRADE_THROUGH = 0.01


@dataclass
class WindowData:
    ticker: str
    open_time: datetime
    result: str                     # "yes" | "no"
    strike: float | None
    sigma: float | None             # per-second BTC vol measured before the window opened
    bid: list[float | None]         # index 0..15 (index = minute; 0 unused), forward-filled YES bid closes
    ask: list[float | None]
    bid_hi: list[float | None]
    ask_lo: list[float | None]
    btc: list[float | None]         # BTC price at the end of each minute

    def sides(self, m: int) -> Sides:
        return Sides(self.bid[m], self.ask[m])

    def side_bid_high(self, side: str, m: int) -> float | None:
        if side == "YES":
            return self.bid_hi[m] if self.bid_hi[m] is not None else self.bid[m]
        lo = self.ask_lo[m] if self.ask_lo[m] is not None else self.ask[m]
        return None if lo is None else 1 - lo

    def fair(self, m: int) -> float | None:
        return fair_yes(self.btc[m], self.strike, (15 - m) * 60, self.sigma)


@dataclass(frozen=True)
class Strategy:
    side: str                 # "underdog" | "favorite" | "model" (whichever side the BTC model says is cheap)
    entry_from: int
    entry_to: int
    lo: float                 # price band for the side we buy
    hi: float
    exit_min: int
    tp_mode: str | None       # None | "abs" | "delta"
    tp: float | None
    min_edge: float | None    # require model edge (after fee) at entry

    @property
    def key(self) -> str:
        tp = "none" if self.tp_mode is None else f"{self.tp_mode}{self.tp:.2f}"
        e = "-" if self.min_edge is None else f"{self.min_edge:.2f}"
        return f"{self.side}|m{self.entry_from}-{self.entry_to}|{self.lo:.2f}-{self.hi:.2f}|x{self.exit_min}|tp:{tp}|edge:{e}"

    def describe(self) -> str:
        who = {"underdog": "the underdog", "favorite": "the favorite", "model": "whichever side live BTC says is underpriced"}[self.side]
        when = f"minute {self.entry_from}" if self.entry_from == self.entry_to else f"minutes {self.entry_from}-{self.entry_to}"
        tp = ("hold to the exit time" if self.tp_mode is None else
              f"sell at {self.tp*100:.0f}¢" if self.tp_mode == "abs" else f"sell {self.tp*100:.0f}¢ above entry")
        e = "" if self.min_edge is None else f", only when live BTC gives at least {self.min_edge*100:.0f}¢ of edge after fees"
        return (f"Buy {who} in {when} priced {self.lo*100:.0f}-{self.hi*100:.0f}¢{e}; {tp}; "
                f"otherwise exit at minute {self.exit_min}.")


PLAYBOOK = Strategy("underdog", 5, 8, 0.25, 0.40, 12, "abs", 0.50, None)          # Mason's current approach
PLAYBOOK_ALIGNED = Strategy("underdog", 5, 8, 0.25, 0.40, 12, "abs", 0.50, 0.03)  # same, only when real BTC agrees


@dataclass
class Trade:
    ticker: str
    side: str
    entry_min: int
    entry: float
    exit: float
    exit_min: int
    how: str
    pnl: float     # per contract, after fees
    ret: float     # pnl / cost (return on the money at risk)


def entry_at(st: Strategy, sd: Sides, fair: float | None) -> tuple[str, float] | None:
    """Would this rule buy right now, given current prices and the BTC model? Returns (side, ask)."""
    if st.side == "model":
        cands = []
        for side in ("YES", "NO"):
            a = sd.ask(side)
            e = edge(fair, side, a)
            if a is not None and e is not None and st.lo <= a <= st.hi and e >= (st.min_edge or 0.0):
                cands.append((e, side, a))
        if not cands:
            return None
        _, side, a = max(cands)
        return side, a
    u = sd.underdog()
    if u is None:
        return None
    side = u if st.side == "underdog" else ("NO" if u == "YES" else "YES")
    a = sd.ask(side)
    if a is None or not (st.lo <= a <= st.hi) or not (0.02 <= a <= 0.98):
        return None
    if st.min_edge is not None:
        e = edge(fair, side, a)
        if e is None or e < st.min_edge:
            return None
    return side, a


def _entry(st: Strategy, w: WindowData) -> tuple[str, int, float] | None:
    need_fair = st.side == "model" or st.min_edge is not None
    for m in range(st.entry_from, st.entry_to + 1):
        hit = entry_at(st, w.sides(m), w.fair(m) if need_fair else None)
        if hit:
            return hit[0], m, hit[1]
    return None


def simulate(st: Strategy, w: WindowData) -> Trade | None:
    ent = _entry(st, w)
    if ent is None:
        return None
    side, m0, a = ent
    cost = a + fee_per_contract(a)
    target = None if st.tp_mode is None else (st.tp if st.tp_mode == "abs" else a + st.tp)
    if target is not None:
        target = min(target, 0.97)
    x_min = min(st.exit_min, LAST_EXIT_MINUTE)
    for k in range(m0 + 1, x_min + 1):
        hi = w.side_bid_high(side, k)
        if target is not None and hi is not None and hi >= target + TP_TRADE_THROUGH - 1e-9:
            pnl = target - cost
            return Trade(w.ticker, side, m0, a, target, k, "TAKE_PROFIT", pnl, pnl / cost)
    b = w.sides(x_min).bid(side)
    b = 0.01 if b is None or b <= 0 else b
    pnl = b - fee_per_contract(b) - cost
    return Trade(w.ticker, side, m0, a, b, x_min, "TIME_EXIT", pnl, pnl / cost)


@dataclass
class Stats:
    n: int = 0
    mean_ret: float = 0.0
    std_ret: float = 0.0
    t: float = 0.0
    win_rate: float = 0.0
    take_profit_rate: float = 0.0
    avg_entry: float = 0.0
    avg_pnl_cents: float = 0.0
    total_ret: float = 0.0


def stats(trades: list[Trade]) -> Stats:
    n = len(trades)
    if n == 0:
        return Stats()
    r = [t.ret for t in trades]
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    return Stats(n=n, mean_ret=mean, std_ret=sd, t=(mean / (sd / math.sqrt(n))) if sd > 0 else 0.0,
                 win_rate=sum(1 for t in trades if t.pnl > 0) / n,
                 take_profit_rate=sum(1 for t in trades if t.how == "TAKE_PROFIT") / n,
                 avg_entry=sum(t.entry for t in trades) / n, avg_pnl_cents=100 * sum(t.pnl for t in trades) / n,
                 total_ret=sum(r))


def grid() -> list[Strategy]:
    out: list[Strategy] = []
    entries = [(m, m) for m in range(2, 11)] + [(3, 6), (5, 8), (6, 9)]
    bands = {"underdog": [(0.10, 0.25), (0.20, 0.35), (0.25, 0.40), (0.30, 0.45)],
             "favorite": [(0.55, 0.70), (0.65, 0.80), (0.75, 0.90)],
             "model": [(0.05, 0.95), (0.20, 0.80)]}
    tps: list[tuple[str | None, float | None]] = [(None, None), ("abs", 0.50), ("delta", 0.10), ("delta", 0.20)]
    for side, bl in bands.items():
        edges: list[float | None] = [0.03, 0.06] if side == "model" else [None, 0.03]
        for ef, et in entries:
            exits = sorted({x for x in (et + 2, et + 4, 12, 13) if et < x <= LAST_EXIT_MINUTE})
            for lo, hi in bl:
                for x in exits:
                    for mode, tp in tps:
                        for me in edges:
                            out.append(Strategy(side, ef, et, lo, hi, x, mode, tp, me))
    return out


@dataclass
class LabResult:
    windows: int
    train_windows: int
    test_windows: int
    split_at: str | None
    tested: int
    rows: list[dict[str, Any]] = field(default_factory=list)


def _row(st: Strategy, tr: Stats, te: Stats, proven: bool, label: str | None = None) -> dict[str, Any]:
    return {"key": st.key, "label": label, "describe": st.describe(), "params": asdict(st),
            "train": asdict(tr), "test": asdict(te), "proven": proven}


def is_proven(tr: Stats, te: Stats) -> bool:
    return tr.n >= 60 and tr.mean_ret > 0 and te.n >= 30 and te.mean_ret > 0 and te.t >= 1.65


def run_lab(windows: list[WindowData], top: int = 25) -> LabResult:
    ws = sorted(windows, key=lambda w: w.open_time)
    cut = int(len(ws) * 0.7)
    train, test = ws[:cut], ws[cut:]
    res = LabResult(windows=len(ws), train_windows=len(train), test_windows=len(test),
                    split_at=test[0].open_time.isoformat() if test else None, tested=0)
    scored: list[tuple[float, Strategy, Stats]] = []
    for st in grid():
        res.tested += 1
        tr = stats([t for w in train if (t := simulate(st, w))])
        if tr.n >= 60 and tr.mean_ret > 0:
            scored.append((tr.t, st, tr))
    scored.sort(key=lambda x: -x[0])
    seen: set[tuple] = set()
    for _, st, tr in scored:  # many rules make identical trades; keep the first of each distinct result
        if len(res.rows) >= top:
            break
        sig = (tr.n, round(tr.mean_ret, 6), round(tr.win_rate, 6))
        if sig in seen:
            continue
        seen.add(sig)
        te = stats([t for w in test if (t := simulate(st, w))])
        res.rows.append(_row(st, tr, te, is_proven(tr, te)))
    for label, st in (("Your playbook", PLAYBOOK), ("Your playbook + BTC agrees", PLAYBOOK_ALIGNED)):
        tr = stats([t for w in train if (t := simulate(st, w))])
        te = stats([t for w in test if (t := simulate(st, w))])
        res.rows.append(_row(st, tr, te, is_proven(tr, te), label))
    res.rows.sort(key=lambda r: (not r["proven"], -(r["test"]["mean_ret"] if r["test"]["n"] else -9)))
    return res


# ------------------------------------------------------------------ studies
def calibration(windows: list[WindowData], minutes: tuple[int, ...] = (3, 5, 7, 9, 11, 13, 14)) -> list[dict[str, Any]]:
    """Does a YES price of X¢ at minute m actually win X% of the time?"""
    out = []
    for m in minutes:
        buckets: dict[int, list[tuple[float, int]]] = {}
        for w in windows:
            mid = w.sides(m).mid()
            if mid is None:
                continue
            buckets.setdefault(min(int(mid * 10), 9), []).append((mid, 1 if w.result == "yes" else 0))
        for b in sorted(buckets):
            v = buckets[b]
            out.append({"minute": m, "bucket": f"{b*10}-{b*10+10}¢", "n": len(v),
                        "avg_price": sum(x for x, _ in v) / len(v), "yes_rate": sum(y for _, y in v) / len(v)})
    return out


def late_flips(windows: list[WindowData]) -> list[dict[str, Any]]:
    """How often a market that looks decided loses in the final minutes."""
    out = []
    for m, label in ((12, "3 min left"), (13, "2 min left"), (14, "1 min left")):
        for thr in (0.80, 0.90):
            n = lost = 0
            for w in windows:
                mid = w.sides(m).mid()
                if mid is None:
                    continue
                if mid >= thr:
                    n += 1
                    lost += w.result != "yes"
                elif mid <= 1 - thr:
                    n += 1
                    lost += w.result != "no"
            out.append({"when": label, "favorite_at_least": f"{thr*100:.0f}¢", "n": n,
                        "favorite_lost_rate": lost / n if n else None,
                        "implied_loss_rate": 1 - thr})
    return out


def underdog_bounce(windows: list[WindowData]) -> dict[str, Any]:
    """Mason's question: when the underdog is 25-40¢ in minutes 5-8, how often does it trade through 50¢ before minute 12?"""
    n = hit = 0
    leftovers: list[float] = []
    for w in windows:
        ent = _entry(PLAYBOOK, w)
        if not ent:
            continue
        side, m0, _ = ent
        n += 1
        if any((h := w.side_bid_high(side, k)) is not None and h >= 0.51 - 1e-9 for k in range(m0 + 1, 13)):
            hit += 1
        else:
            b = w.sides(12).bid(side)
            if b is not None:
                leftovers.append(b)
    leftovers.sort()
    return {"n": n, "reached_50c_rate": hit / n if n else None,
            "median_bid_at_min12_when_no_flip": leftovers[len(leftovers) // 2] if leftovers else None}


def model_vs_market(windows: list[WindowData], m: int = 7) -> dict[str, Any]:
    """Brier score (lower is better) of the BTC model vs the Kalshi price, at minute m."""
    bm = bk = 0.0
    n = 0
    for w in windows:
        f, mid = w.fair(m), w.sides(m).mid()
        if f is None or mid is None:
            continue
        y = 1.0 if w.result == "yes" else 0.0
        bm += (f - y) ** 2
        bk += (mid - y) ** 2
        n += 1
    return {"minute": m, "n": n, "brier_model": bm / n if n else None, "brier_market": bk / n if n else None}
