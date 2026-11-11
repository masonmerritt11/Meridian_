"""Market analysts: trend, statistical arbitrage, volatility, liquidity and the cross-asset regime desk.
All deterministic, computed from stored closed candles and order-book snapshots."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from meridian.analysts import calcs as c
from meridian.analysts.base import AnalystOutput, AnalystWorker, Assessment, MarketContext, latest_by_worker
from meridian.core.util import utcnow
from meridian.db.models import BookSnapshot
from meridian.marketdata.types import ASSETS
from meridian.workers.base import RunContext, WorkerSpec
from meridian.workers.indicators import atr, ema


def spec(wid: str, dept: str, name: str, resp: str, every: int = 3600, esc: str = "exec.research_officer") -> WorkerSpec:
    return WorkerSpec(id=wid, department=dept, name=name, responsibility=resp, version="1.0.0",
                      output_model=AnalystOutput, schedule_seconds=every, escalation_to=esc, max_retries=2)


# ======================================================================= trend department
class BreakoutSpecialist(AnalystWorker):
    spec = spec("trend.breakout_specialist", "trend", "Breakout Specialist",
                "Detects volatility compression (Bollinger width) and breakouts from it.", esc="trend.portfolio_lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl = ctx.closes(a)
            widths = c.bollinger_width(cl)
            if len(widths) < 50:
                return None
            pct = c.percentile_rank(widths[-200:], widths[-1])
            prior = widths[-6:-1]
            was_tight = min(prior) <= sorted(widths[-200:])[int(len(widths[-200:]) * 0.2)]
            hi20, lo20 = max(ctx.highs(a)[-21:-1]), min(ctx.lows(a)[-21:-1])
            if was_tight and cl[-1] > hi20:
                return Assessment(a, "BREAKOUT_UP", f"Broke above a {pct:.0%}-width squeeze range ({hi20:g}).", 0.6, 0.6,
                                  {"width_pct": round(pct, 3)})
            if was_tight and cl[-1] < lo20:
                return Assessment(a, "BREAKOUT_DOWN", f"Broke below a squeeze range ({lo20:g}).", -0.6, 0.6, {"width_pct": round(pct, 3)})
            if pct <= 0.2:
                return Assessment(a, "COMPRESSION", f"Volatility compressed (band width at {pct:.0%} percentile); breakout setup forming.",
                                  0.0, 0.5, {"width_pct": round(pct, 3)})
            return Assessment(a, "NORMAL", f"Band width at {pct:.0%} percentile; no squeeze.", 0.0, 0.3, {"width_pct": round(pct, 3)})
        return self.per_asset(ctx, one)


class MomentumRanker(AnalystWorker):
    spec = spec("trend.momentum_ranker", "trend", "Momentum Ranker",
                "Ranks the six assets by volatility-adjusted 1-day and 5-day momentum.", esc="trend.portfolio_lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        raw = {}
        for a in ctx.instruments:
            cl = ctx.closes(a)
            if len(cl) < 130:
                continue
            vol = c.realized_vol(cl, 120, 1) or 0
            if vol <= 0:
                continue
            m = 0.5 * ((cl[-1] / cl[-25] - 1) / (vol * 24 ** 0.5)) + 0.5 * ((cl[-1] / cl[-121] - 1) / (vol * 120 ** 0.5))
            raw[a] = m
        if len(raw) < 2:
            return []
        ranked = sorted(raw, key=raw.get, reverse=True)
        out = []
        for i, a in enumerate(ranked):
            score = c.clamp(1 - 2 * i / (len(ranked) - 1)) * 0.6
            out.append(Assessment(a, f"RANK_{i + 1}_OF_{len(ranked)}",
                                  f"Momentum rank {i + 1} of {len(ranked)} (vol-adjusted score {raw[a]:+.2f}).",
                                  score, 0.5, {"momentum": round(raw[a], 4), "rank": i + 1}))
        return out


class MultiTimeframe(AnalystWorker):
    spec = spec("trend.multi_timeframe", "trend", "Multi-Timeframe Analyst",
                "Checks trend agreement on 1-hour, 4-hour and daily views.", esc="trend.portfolio_lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            al = c.mtf_alignment(ctx.closes(a))
            vals = [v for v in al.values() if v is not None]
            if not vals:
                return None
            sc = sum(vals) / len(al)
            label = "ALIGNED_UP" if all(v == 1 for v in vals) and len(vals) == 3 else \
                "ALIGNED_DOWN" if all(v == -1 for v in vals) and len(vals) == 3 else "MIXED"
            txt = ", ".join(f"{k} {'up' if v == 1 else 'down' if v == -1 else 'n/a'}" for k, v in al.items())
            return Assessment(a, label, f"Timeframes: {txt}.", sc * 0.7, 0.6, {"alignment": al})
        return self.per_asset(ctx, one)


class ExhaustionMonitor(AnalystWorker):
    spec = spec("trend.exhaustion_monitor", "trend", "Trend Exhaustion Monitor",
                "Flags overextended moves: RSI extremes and distance from the 20-bar average.", esc="trend.portfolio_lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl, hi, lo = ctx.closes(a), ctx.highs(a), ctx.lows(a)
            r = c.rsi(cl)
            at = atr(hi, lo, cl)
            e20 = ema(cl, 20)
            if r is None or not at or not e20:
                return None
            ext = (cl[-1] - e20[-1]) / at[-1]
            if r >= 75 or ext >= 3:
                return Assessment(a, "OVERBOUGHT", f"RSI {r:.0f}, price {ext:+.1f} ATR from EMA20: late to chase longs.", -0.5, 0.5,
                                  {"rsi": round(r, 1), "ext_atr": round(ext, 2)})
            if r <= 25 or ext <= -3:
                return Assessment(a, "OVERSOLD", f"RSI {r:.0f}, price {ext:+.1f} ATR from EMA20: late to chase shorts.", 0.5, 0.5,
                                  {"rsi": round(r, 1), "ext_atr": round(ext, 2)})
            return Assessment(a, "NOT_EXTENDED", f"RSI {r:.0f}, {ext:+.1f} ATR from EMA20.", 0.0, 0.4,
                              {"rsi": round(r, 1), "ext_atr": round(ext, 2)})
        return self.per_asset(ctx, one)


def _index_price(inst) -> float | None:  # noqa: ANN001
    v = ((inst.raw or {}).get("future_product_details") or {}).get("index_price")
    try:
        return float(v) if v not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None


class CarryRoll(AnalystWorker):
    spec = spec("trend.carry_roll", "trend", "Carry & Roll Analyst",
                "Annualized futures premium vs index (carry) and roll timing before expiry.", esc="trend.portfolio_lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            cl = ctx.closes(a)
            if not cl or inst.contract_expiry is None:
                continue
            days = (inst.contract_expiry - utcnow()).total_seconds() / 86400
            idx = _index_price(inst)
            roll = days <= 5
            if idx is None or days <= 0:
                out.append(Assessment(a, "ROLL_SOON" if roll else "CARRY_UNKNOWN",
                                      f"{days:.1f} days to expiry; index price unavailable for carry.", None, None,
                                      {"days_to_expiry": round(days, 2)}, "OPPOSES" if roll else None))
                continue
            prem = cl[-1] / idx - 1
            ann = prem * 365 / max(days, 1)
            score = c.clamp(-ann / 0.4) * 0.4  # steep contango taxes longs, backwardation pays them
            label = "ROLL_SOON" if roll else ("CONTANGO" if prem > 0 else "BACKWARDATION")
            out.append(Assessment(a, label, f"Futures {prem:+.2%} vs index ({ann:+.1%} annualized); {days:.1f} days to expiry.",
                                  score, 0.4, {"premium": round(prem, 5), "annualized": round(ann, 4),
                                               "days_to_expiry": round(days, 2)}, "OPPOSES" if roll else None))
        return out


class DepartmentLead(AnalystWorker):
    """Consensus of a department's latest per-asset scores (confidence-weighted)."""
    members: tuple[str, ...] = ()

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a in ctx.instruments:
            rows = [r for r in latest_by_worker(s, a, timedelta(hours=3)) if r.worker_id in self.members and r.score is not None]
            if not rows:
                continue
            w = [float(r.confidence or 0.5) for r in rows]
            sc = sum(float(r.score) * wi for r, wi in zip(rows, w, strict=True)) / sum(w)
            label = "BULLISH" if sc >= 0.2 else "BEARISH" if sc <= -0.2 else "NEUTRAL"
            out.append(Assessment(a, label, f"{len(rows)} analysts; consensus {sc:+.2f} ("
                                  + ", ".join(f"{r.worker_id.split('.')[1]} {float(r.score):+.1f}" for r in rows) + ")",
                                  sc, min(1.0, len(rows) / len(self.members)), {"members": {r.worker_id: float(r.score) for r in rows}}))
        return out


class TrendLead(DepartmentLead):
    spec = spec("trend.portfolio_lead", "trend", "Trend Portfolio Lead", "Trend department consensus per asset.", 3600)
    members = ("trend.breakout_specialist", "trend.momentum_ranker", "trend.multi_timeframe",
               "trend.exhaustion_monitor", "trend.carry_roll")


# ======================================================================= statistical arbitrage
PAIRS = (("GOLD", "SILVER"), ("BTC", "ETH"))


class PairsAnalyst(AnalystWorker):
    spec = spec("stat_arb.pairs", "stat_arb", "Pairs Analyst", "Gold/silver and BTC/ETH ratio z-scores.", esc="stat_arb.lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for x, y in PAIRS:
            cx, cy = ctx.closes(x), ctx.closes(y)
            n = min(len(cx), len(cy))
            if n < 130:
                continue
            ratio = [a / b for a, b in zip(cx[-n:], cy[-n:], strict=True) if b]
            z = c.zscore(ratio, 120)
            if z is None:
                continue
            tilt = c.clamp(z / 2.5) * 0.4  # high ratio: x rich vs y
            data = {"pair": f"{x}/{y}", "ratio": round(ratio[-1], 4), "z": round(z, 2)}
            note = f"{x}/{y} ratio {ratio[-1]:.3f}, z {z:+.2f} vs 5-day mean"
            out.append(Assessment(x, "RICH_VS_PAIR" if z > 1.5 else "CHEAP_VS_PAIR" if z < -1.5 else "FAIR_VS_PAIR",
                                  note, -tilt, 0.4, data))
            out.append(Assessment(y, "CHEAP_VS_PAIR" if z > 1.5 else "RICH_VS_PAIR" if z < -1.5 else "FAIR_VS_PAIR",
                                  note, tilt, 0.4, data))
        return out


class Cointegration(AnalystWorker):
    spec = spec("stat_arb.cointegration", "stat_arb", "Cointegration Researcher",
                "Engle-Granger test, hedge ratio and half-life for gold/silver and BTC/ETH.", 6 * 3600, "stat_arb.lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        import math
        out = []
        for x, y in PAIRS:
            cx, cy = ctx.closes(x), ctx.closes(y)
            n = min(len(cx), len(cy))
            if n < 200:
                continue
            lx, ly = [math.log(v) for v in cx[-n:]], [math.log(v) for v in cy[-n:]]
            a, b = c.ols(lx, ly)
            resid = [p - (a + b * q) for p, q in zip(lx, ly, strict=True)]
            t = c.adf_tstat(resid)
            hl = c.half_life(resid)
            coint = t is not None and t < -3.4
            out.append(Assessment(f"{x}/{y}", "COINTEGRATED" if coint else "NOT_COINTEGRATED",
                                  f"Hedge ratio {b:.3f}; residual DF t-stat {t:.2f} (≈-3.4 needed)"
                                  + (f"; half-life {hl:.0f}h" if hl else ""), None, 0.5,
                                  {"beta": round(b, 4), "t_stat": round(t, 3) if t else None, "half_life_h": round(hl, 1) if hl else None,
                                   "bars": n}))
        return out


class MeanReversion(AnalystWorker):
    spec = spec("stat_arb.mean_reversion", "stat_arb", "Mean-Reversion Analyst",
                "Short-horizon stretch: z-score of price vs its 20-bar mean.", esc="stat_arb.lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            z = c.zscore(ctx.closes(a), 20)
            if z is None:
                return None
            if abs(z) >= 2:
                return Assessment(a, "STRETCHED_UP" if z > 0 else "STRETCHED_DOWN",
                                  f"Price {z:+.1f} standard deviations from its 20-hour mean; snap-back risk.", -c.clamp(z / 4) * 0.5, 0.4,
                                  {"z": round(z, 2)})
            return Assessment(a, "IN_RANGE", f"Price {z:+.1f} sd from its 20-hour mean.", 0.0, 0.3, {"z": round(z, 2)})
        return self.per_asset(ctx, one)


class BasisAnalyst(AnalystWorker):
    spec = spec("stat_arb.basis", "stat_arb", "Basis Spread Analyst",
                "Futures premium/discount to the exchange index price.", esc="stat_arb.lead")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            cl, idx = ctx.closes(a), _index_price(inst)
            if not cl or idx is None:
                continue
            bps = (cl[-1] / idx - 1) * 10_000
            out.append(Assessment(a, "PREMIUM" if bps > 0 else "DISCOUNT",
                                  f"Futures {bps:+.0f} bps vs index {idx:g} (index from last discovery).", None, 0.3,
                                  {"basis_bps": round(bps, 1), "index_price": idx}))
        return out


class StatArbLead(DepartmentLead):
    spec = spec("stat_arb.lead", "stat_arb", "Stat Arb Lead", "Stat-arb department consensus per asset.", 3600)
    members = ("stat_arb.pairs", "stat_arb.mean_reversion")


# ======================================================================= volatility
class RealizedVol(AnalystWorker):
    spec = spec("vol.realized", "volatility", "Realized Volatility Analyst", "ATR% and 1-day / 7-day realized volatility.")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl = ctx.closes(a)
            v1, v7 = c.realized_vol(cl, 24), c.realized_vol(cl, 168)
            ap = c.atr_pct(ctx.highs(a), ctx.lows(a), cl)
            if v1 is None or v7 is None:
                return None
            lab = "EXPANDING" if v1 > 1.3 * v7 else "CONTRACTING" if v1 < 0.7 * v7 else "STABLE"
            return Assessment(a, lab, f"Realized vol 1d {v1:.0%}, 7d {v7:.0%} annualized; ATR {ap:.2f}%.", None, 0.6,
                              {"rv_1d": round(v1, 4), "rv_7d": round(v7, 4), "atr_pct": round(ap or 0, 4)})
        return self.per_asset(ctx, one)


class RegimeClassifier(AnalystWorker):
    spec = spec("vol.regime", "volatility", "Regime Classifier",
                "Trending vs ranging vs high-volatility regime (efficiency ratio + volatility percentile).")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl = ctx.closes(a)
            er = c.efficiency_ratio(cl, 48)
            rets = [abs(x) for x in c.returns(cl)]
            if er is None or len(rets) < 100:
                return None
            vol_now = mean(rets[-24:])
            pct = c.percentile_rank([mean(rets[i - 24:i]) for i in range(24, len(rets))], vol_now)
            if pct >= 0.9:
                return Assessment(a, "HIGH_VOL", f"Volatility at {pct:.0%} percentile; whipsaw risk for breakouts.", None, 0.6,
                                  {"efficiency": round(er, 3), "vol_pct": round(pct, 3)}, "OPPOSES")
            if er >= 0.35:
                return Assessment(a, "TRENDING", f"Efficient directional movement (ER {er:.2f}); trend-following conditions.", None, 0.6,
                                  {"efficiency": round(er, 3), "vol_pct": round(pct, 3)}, "SUPPORTS")
            if er <= 0.15:
                return Assessment(a, "RANGING", f"Choppy, range-bound action (ER {er:.2f}); breakouts often fail here.", None, 0.6,
                                  {"efficiency": round(er, 3), "vol_pct": round(pct, 3)}, "OPPOSES")
            return Assessment(a, "TRANSITIONAL", f"Mixed conditions (ER {er:.2f}).", None, 0.4,
                              {"efficiency": round(er, 3), "vol_pct": round(pct, 3)})
        return self.per_asset(ctx, one)


class VolForecaster(AnalystWorker):
    spec = spec("vol.forecast", "volatility", "Volatility Forecaster", "EWMA (RiskMetrics) next-day volatility vs recent average.")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl = ctx.closes(a)
            f = c.ewma_vol(cl)
            base = c.realized_vol(cl, 168, 1)
            if f is None or not base:
                return None
            ratio = f / base
            lab = "RISING" if ratio > 1.25 else "FALLING" if ratio < 0.8 else "STEADY"
            return Assessment(a, lab, f"Forecast hourly vol {f:.3%} vs 7-day {base:.3%} ({ratio:.2f}x).", None, 0.5,
                              {"ewma_hourly": round(f, 6), "ratio": round(ratio, 3)}, "OPPOSES" if ratio > 1.6 else None)
        return self.per_asset(ctx, one)


class TailRisk(AnalystWorker):
    spec = spec("vol.tail", "volatility", "Tail Risk Analyst", "Largest hourly shocks vs ATR: gap-through-stop risk.",
                esc="risk.chief_risk_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        def one(a):  # noqa: ANN001, ANN202
            cl, hi, lo = ctx.closes(a), ctx.highs(a), ctx.lows(a)
            at = atr(hi, lo, cl)
            if not at or len(cl) < 170:
                return None
            worst = max(hi[i] - lo[i] for i in range(len(cl) - 168, len(cl)))
            ratio = worst / at[-1]
            lab = "FAT_TAILS" if ratio >= 4 else "NORMAL_TAILS"
            return Assessment(a, lab, f"Largest 1-hour range this week = {ratio:.1f}x ATR"
                              + ("; a 2xATR stop can be gapped through." if ratio >= 4 else "."), None, 0.5,
                              {"worst_range_atr": round(ratio, 2)}, "OPPOSES" if ratio >= 5 else None)
        return self.per_asset(ctx, one)


class CorrelationAnalyst(AnalystWorker):
    spec = spec("vol.correlation", "volatility", "Correlation Analyst", "7-day hourly-return correlation matrix across the six assets.",
                esc="risk.chief_risk_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        rets = {a: c.returns(ctx.closes(a))[-168:] for a in ctx.instruments}
        rets = {a: r for a, r in rets.items() if len(r) >= 48}
        assets = sorted(rets)
        if len(assets) < 2:
            return []
        m = {a: {b: (round(c.correlation(rets[a], rets[b]) or 0, 3) if a != b else 1.0) for b in assets} for a in assets}
        pairs = sorted(((m[a][b], a, b) for i, a in enumerate(assets) for b in assets[i + 1:]), reverse=True)
        top = ", ".join(f"{a}-{b} {v:+.2f}" for v, a, b in pairs[:3])
        return [Assessment("MARKET", "CORRELATION_MATRIX", f"Most correlated: {top}.", None, 0.6, {"matrix": m})]


# ======================================================================= liquidity
def _books(s: Session, inst_id: str, hours: int = 24) -> list[BookSnapshot]:
    return list(s.execute(select(BookSnapshot).where(BookSnapshot.instrument_id == inst_id,
                                                     BookSnapshot.ts >= utcnow() - timedelta(hours=hours))
                          .order_by(desc(BookSnapshot.ts))).scalars())


class OrderBookAnalyst(AnalystWorker):
    spec = spec("liquidity.order_book", "liquidity", "Order Book Analyst", "Top-25 depth imbalance from the latest book snapshot.",
                esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            b = _books(s, inst.id, 1)
            if not b or not b[0].bid_depth or not b[0].ask_depth:
                continue
            bd, ad = float(b[0].bid_depth), float(b[0].ask_depth)
            imb = (bd - ad) / (bd + ad)
            out.append(Assessment(a, "BID_HEAVY" if imb > 0.3 else "ASK_HEAVY" if imb < -0.3 else "BALANCED",
                                  f"Depth imbalance {imb:+.2f} (bids {bd:g} vs asks {ad:g}).", c.clamp(imb) * 0.2, 0.2,
                                  {"imbalance": round(imb, 3)}))
        return out


class SpreadMonitor(AnalystWorker):
    spec = spec("liquidity.spread_monitor", "liquidity", "Spread Monitor", "Current spread vs its 24-hour median.",
                every=900, esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            sp = [float(b.spread_bps) for b in _books(s, inst.id) if b.spread_bps is not None]
            if not sp:
                continue
            med = median(sp)
            ratio = sp[0] / med if med else 1
            out.append(Assessment(a, "WIDE" if ratio >= 2 else "NORMAL", f"Spread {sp[0]:.1f} bps vs 24h median {med:.1f} bps.",
                                  None, 0.5, {"spread_bps": sp[0], "median_bps": med}, "OPPOSES" if ratio >= 3 else None))
        return out


class LiquidityScorer(AnalystWorker):
    spec = spec("liquidity.scorer", "liquidity", "Liquidity Scorer", "0-100 liquidity score from spread, depth and volume.",
                esc="risk.chief_risk_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            b = _books(s, inst.id, 2)
            bars = ctx.bars(a)
            if not b or b[0].spread_bps is None or len(bars) < 24:
                continue
            spread = float(b[0].spread_bps)
            vol24 = sum(float(x.volume) for x in bars[-24:])
            score = max(0.0, 100 - spread * 4) * 0.7 + min(30.0, vol24 / 100)
            lab = "LIQUID" if score >= 60 else "ADEQUATE" if score >= 35 else "THIN"
            out.append(Assessment(a, lab, f"Liquidity {score:.0f}/100 (spread {spread:.1f} bps, 24h volume {vol24:,.0f} contracts).",
                                  None, 0.5, {"score": round(score, 1), "volume_24h": vol24}, "OPPOSES" if lab == "THIN" else None))
        return out


class SessionAnalyst(AnalystWorker):
    spec = spec("liquidity.sessions", "liquidity", "Session Analyst", "Which trading session is active and what liquidity to expect.",
                every=1800, esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        now = utcnow()
        h, wd = now.hour, now.weekday()
        if wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21):
            sess, thin = "WEEKEND", True
        elif 13 <= h < 16:
            sess, thin = "LONDON_NY_OVERLAP", False
        elif 7 <= h < 13:
            sess, thin = "LONDON", False
        elif 16 <= h < 21:
            sess, thin = "NEW_YORK", False
        else:
            sess, thin = "ASIA", True
        out = []
        for a in ctx.instruments:
            crypto = ASSETS[a].asset_class == "CRYPTO"
            note = "crypto trades around the clock" if crypto else ("commodity futures thin out" if thin else "main commodity hours")
            out.append(Assessment(a, sess, f"{sess.replace('_', ' ').title()} session (UTC {h:02d}:00); {note}.", None, 0.3,
                                  {"session": sess}, "OPPOSES" if (thin and not crypto and sess == "WEEKEND") else None))
        return out


class TradingCostAnalyst(AnalystWorker):
    spec = spec("liquidity.cost", "liquidity", "Trading Cost Analyst",
                "Round-trip spread cost as a share of the planned risk (fees not included: not published).",
                esc="risk.chief_risk_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a, inst in ctx.instruments.items():
            b = _books(s, inst.id, 2)
            cl, hi, lo = ctx.closes(a), ctx.highs(a), ctx.lows(a)
            at = atr(hi, lo, cl)
            if not b or b[0].best_bid is None or b[0].best_ask is None or not at:
                continue
            spread = float(b[0].best_ask - b[0].best_bid)
            share = spread / (2 * at[-1])
            out.append(Assessment(a, "COSTLY" if share > 0.1 else "CHEAP",
                                  f"Crossing the spread costs {share:.1%} of a 2xATR stop distance (exchange fees not included).",
                                  None, 0.5, {"cost_share_of_risk": round(share, 4)}, "OPPOSES" if share > 0.15 else None))
        return out


# ======================================================================= cross-asset regime (global macro)
class CrossAssetRegime(AnalystWorker):
    spec = spec("macro.regime", "global_macro", "Cross-Asset Regime Analyst",
                "Risk-on / risk-off read from crypto vs gold performance and cross-asset volatility.", esc="exec.chief_macro_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        cb, cg = ctx.closes("BTC"), ctx.closes("GOLD")
        if len(cb) < 121 or len(cg) < 121:
            return []
        rb, rg = cb[-1] / cb[-121] - 1, cg[-1] / cg[-121] - 1
        spread = rb - rg
        lab = "RISK_ON" if spread > 0.03 else "RISK_OFF" if spread < -0.03 else "MIXED"
        tilt = c.clamp(spread / 0.1) * 0.4
        bias = {"BTC": tilt, "ETH": tilt, "XRP": tilt, "GOLD": -tilt * 0.75, "SILVER": -tilt * 0.25, "OIL": tilt * 0.25}
        summ = f"5-day BTC {rb:+.1%} vs gold {rg:+.1%}: {lab.replace('_', ' ').lower()}."
        out = [Assessment("MARKET", lab, summ, None, 0.5, {"btc_5d": round(rb, 4), "gold_5d": round(rg, 4)})]
        out += [Assessment(a, lab, summ, bias[a], 0.4, {"tilt": round(bias[a], 3)}) for a in ctx.instruments if a in bias]
        return out


MARKET_ANALYSTS = [BreakoutSpecialist, MomentumRanker, MultiTimeframe, ExhaustionMonitor, CarryRoll, TrendLead,
                   PairsAnalyst, Cointegration, MeanReversion, BasisAnalyst, StatArbLead,
                   RealizedVol, RegimeClassifier, VolForecaster, TailRisk, CorrelationAnalyst,
                   OrderBookAnalyst, SpreadMonitor, LiquidityScorer, SessionAnalyst, TradingCostAnalyst, CrossAssetRegime]

_ = (defaultdict, MarketContext, RunContext)
