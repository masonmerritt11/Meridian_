"use client";
import { useEffect, useRef, useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Stat } from "@/components/ui";
import { ago, fmtTime, usd, usePoll } from "@/lib/api";

const c = (v: any) => (v === null || v === undefined || v === "" ? "—" : `${Math.round(Number(v) * 100)}¢`);
const pct = (v: any, d = 0) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(d)}%`);
const clock = (s: number) => (s <= 0 ? "0:00" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
const KIND: Record<string, string> = { ALIGNED_UNDERDOG: "Underdog underpriced vs real BTC", STRATEGY: "Strategy setup" };

function useNow(ms = 500) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

function beep(urgent: boolean) {
  try {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    [0, 0.25, ...(urgent ? [0.5] : [])].forEach((t) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = urgent ? 880 : 660; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.15, ctx.currentTime + t); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.2);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.2);
    });
  } catch { /* audio not available */ }
}

function Chart({ ticks }: { ticks: any[] }) {
  if (ticks.length < 2) return <Empty>Collecting the odds for this window…</Empty>;
  const W = 640, H = 160, pad = 4;
  const t0 = new Date(ticks[0].ts).getTime(), t1 = Math.max(new Date(ticks[ticks.length - 1].ts).getTime(), t0 + 1);
  const x = (t: string) => pad + ((new Date(t).getTime() - t0) / (t1 - t0)) * (W - 2 * pad);
  const y = (p: number) => H - pad - p * (H - 2 * pad);
  const line = (f: (k: any) => number | null) => ticks.map((k) => [x(k.ts), f(k)] as const).filter(([, v]) => v !== null)
    .map(([a, v], i) => `${i ? "L" : "M"}${a.toFixed(1)},${y(v as number).toFixed(1)}`).join(" ");
  const mid = (k: any) => (k.yes_bid !== null && k.yes_ask !== null ? (Number(k.yes_bid) + Number(k.yes_ask)) / 2 : null);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="YES price versus BTC fair value">
      <line x1={pad} x2={W - pad} y1={y(0.5)} y2={y(0.5)} stroke="currentColor" strokeOpacity="0.2" strokeDasharray="4 4" />
      <path d={line(mid)} fill="none" stroke="#7aa2ff" strokeWidth="2" />
      <path d={line((k) => (k.fair_yes === null ? null : Number(k.fair_yes)))} fill="none" stroke="#f0b35a" strokeWidth="2" strokeDasharray="5 3" />
      <text x={W - pad} y={y(0.5) - 4} textAnchor="end" fontSize="10" fill="currentColor" opacity="0.5">50¢</text>
    </svg>
  );
}

function Setup({ s, now }: { s: any; now: number }) {
  const left = (new Date(s.exit_by).getTime() - now) / 1000;
  const exitNow = s.status === "EXIT_NOW" || (s.status === "ACTIVE" && left <= 0);
  return (
    <div className="note" style={exitNow ? { borderColor: "#e5484d" } : undefined}>
      <div className="note-head">
        <Badge v={exitNow ? "EXIT_NOW" : s.status} />
        <b>{KIND[s.kind] ?? s.kind}: buy {s.side === "YES" ? "UP (YES)" : "DOWN (NO)"} at {c(s.entry_price)}</b>
        <Badge v={s.proven ? "PROVEN" : "UNPROVEN"} title={s.proven ? "Passed the out-of-sample test" : "History has not confirmed this rule yet"} />
      </div>
      <dl className="kv">
        <dt>Contracts</dt><dd>{s.contracts}</dd>
        <dt>Worst-case loss</dt><dd>{usd(s.max_loss)}</dd>
        <dt>Sell target</dt><dd>{s.take_profit ? c(s.take_profit) : "hold to exit time"}</dd>
        <dt>Exit by</dt><dd>{new Date(s.exit_by).toLocaleTimeString()} {s.status === "ACTIVE" && left > 0 ? `(${clock(left)})` : ""}</dd>
        {s.fair !== null && <><dt>BTC-implied fair</dt><dd>{c(s.fair)} {s.edge !== null && `(edge ${c(s.edge)} after fee)`}</dd></>}
        {s.paper_pnl !== null && <><dt>If followed exactly</dt><dd>{Number(s.paper_pnl) >= 0 ? "+" : ""}{(Number(s.paper_pnl) * 100).toFixed(1)}¢/contract = {usd(Number(s.paper_pnl) * s.contracts)}</dd></>}
      </dl>
      {exitNow && <div className="error" style={{ marginTop: 6 }}>EXIT NOW. Do not hold this position into the close.</div>}
      {s.status === "TAKE_PROFIT" && <div className="notice" style={{ marginTop: 6 }}>Target reached: take profit now.</div>}
      <p className="notice" style={{ marginTop: 6 }}>{s.detail?.why}</p>
    </div>
  );
}

function Stats({ v }: { v: any }) {
  if (!v?.n) return <span className="notice">no trades</span>;
  return <span>{v.n} trades · {pct(v.win_rate)} won · {v.mean_ret >= 0 ? "+" : ""}{pct(v.mean_ret, 1)} avg/trade · t {v.t.toFixed(1)}</span>;
}

export default function Predictions() {
  const live = usePoll<any>("/predictions/live", 2000);
  const res = usePoll<any>("/predictions/research", 60000);
  const now = useNow();
  const [alerts, setAlerts] = useState(false);
  const seen = useRef<Map<number, string> | null>(null);
  const [minute, setMinute] = useState(7);

  useEffect(() => {
    const sigs: any[] = live.data?.signals ?? [];
    if (seen.current === null) { seen.current = new Map(sigs.map((s) => [s.id, s.status])); return; }
    for (const s of sigs) {
      const prev = seen.current.get(s.id);
      if (prev !== s.status && (prev === undefined || s.status !== "CLOSED")) {
        const urgent = s.status === "EXIT_NOW";
        if (alerts) {
          beep(urgent);
          if ("Notification" in window && Notification.permission === "granted") {
            const msg = prev === undefined ? `${KIND[s.kind]}: buy ${s.side} at ${c(s.entry_price)} x${s.contracts}, exit by ${new Date(s.exit_by).toLocaleTimeString()}`
              : urgent ? `EXIT NOW: ${s.side} ${s.ticker}` : `Take profit: ${s.side} at ${c(s.take_profit)}`;
            new Notification("MERIDIAN prediction desk", { body: msg });
          }
        }
      }
      seen.current.set(s.id, s.status);
    }
  }, [live.data, alerts]);

  const d = live.data, w = d?.window, r = res.data;
  const left = w ? Math.max(0, (new Date(w.close_time).getTime() - now) / 1000) : 0;
  const current = (d?.signals ?? []).filter((s: any) => w && s.ticker === w.ticker);
  const past = (d?.signals ?? []).filter((s: any) => !w || s.ticker !== w.ticker).slice(0, 12);
  const lab = r?.lab;
  const studies = lab?.studies;

  return (
    <Shell title="Prediction Desk">
      <ErrorNote error={live.error} />
      {d && !d.enabled && <div className="sim-banner">The prediction desk runs only with live market data (MARKET_DATA_MODE=coinbase).</div>}
      <div className="grid">
        <Card title={w ? `Live window · closes ${new Date(w.close_time).toLocaleTimeString()}` : "Live window"} span={7}
          right={<span className="notice">refresh every {d?.poll_seconds ?? 2}s · {w?.stale ? <Badge v="STALE" /> : w ? `as of ${new Date(w.as_of).toLocaleTimeString()}` : ""}
            {" "}<button className="link" onClick={() => { setAlerts(true); if ("Notification" in window) Notification.requestPermission(); beep(false); }}>{alerts ? "alerts on" : "turn on alerts"}</button></span>}>
          {!w ? <Empty>{d?.desk?.detail?.state === "NO_OPEN_WINDOW" ? "No 15-minute window is open right now." : d?.desk?.detail?.error ? `Desk error: ${d.desk.detail.error}` : "Waiting for the first live read…"}</Empty> : (<>
            <div className="stats">
              <Stat label="Time left" value={clock(left)} tone={left < 120 ? "bad" : left < 240 ? "warn" : undefined}
                sub={left < 240 ? "no new entries now" : `minute ${Math.floor(15 - left / 60)} of 15`} />
              <Stat label="BTC (Coinbase)" value={w.btc ? `$${Number(w.btc).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "—"}
                sub={w.strike && w.btc ? `${Number(w.btc) >= Number(w.strike) ? "+" : ""}${(Number(w.btc) - Number(w.strike)).toFixed(0)} vs strike $${Number(w.strike).toLocaleString()}` : "strike —"} />
              <Stat label="UP (YES)" value={`${c(w.yes_bid)} / ${c(w.yes_ask)}`} sub="bid / ask" />
              <Stat label="DOWN (NO)" value={`${c(w.no_bid)} / ${c(w.no_ask)}`} sub="bid / ask" />
              <Stat label="Fair UP from BTC" value={c(w.fair_yes)} sub="what live BTC implies" />
              <Stat label={`Underdog: ${w.underdog === "YES" ? "UP" : "DOWN"}`} value={w.aligned ? "ALIGNED" : "not aligned"}
                tone={w.aligned ? "ok" : undefined}
                sub={w.underdog_edge === null ? "—" : `Kalshi ${c(w.underdog_ask)} vs fair ${c(w.underdog_fair)} · edge ${c(w.underdog_edge)}`} />
            </div>
            <Chart ticks={d.ticks} />
            <div className="notice"><span style={{ color: "#7aa2ff" }}>━</span> Kalshi UP price &nbsp; <span style={{ color: "#f0b35a" }}>┅</span> fair UP from live BTC. When the gold line sits well above the blue line while UP is the underdog (or well below while DOWN is), the underdog is cheap versus the real market.</div>
          </>)}
        </Card>

        <Card title="Setups this window" span={5} right={<span className="notice">max loss {usd(d?.max_loss_usd)} per setup</span>}>
          {current.length ? current.map((s: any) => <Setup key={s.id} s={s} now={now} />) : <Empty>No setup yet. The desk alerts when the underdog is cheap against live BTC, or when the active strategy's conditions are met.</Empty>}
          <p className="notice">Active strategy {d?.active_strategy?.proven ? <Badge v="PROVEN" /> : <Badge v="UNPROVEN" />}: {d?.active_strategy?.describe}</p>
          <ul className="list">{(d?.rules ?? []).map((x: string) => <li key={x}>{x}</li>)}</ul>
        </Card>

        <Card title="Strategy lab: what actually worked" span={12}
          right={<span className="notice">{lab ? `${lab.tested} rules tested on ${lab.windows} windows · trained on ${lab.train_windows}, verified on the newest ${lab.test_windows} · ${ago(lab.run_at)}` : ""}</span>}>
          {!lab ? <Empty>Waiting for enough history. {r?.coverage ? `${r.coverage.with_odds_history} windows loaded so far.` : ""}</Empty> : (
            <table><thead><tr><th>Rule</th><th>Older windows (tuned on)</th><th>Newest windows (never seen)</th><th>Verdict</th></tr></thead>
              <tbody>{lab.rows.map((x: any) => (
                <tr key={x.key + (x.label ?? "")} style={x.label ? { background: "rgba(240,179,90,0.08)" } : undefined}>
                  <td>{x.label && <b>{x.label}: </b>}{x.describe}</td><td><Stats v={x.train} /></td><td><Stats v={x.test} /></td>
                  <td><Badge v={x.proven ? "PROVEN" : "UNPROVEN"} /></td></tr>))}</tbody></table>)}
          <p className="notice">PROVEN means it made money on windows it was never tuned on, with at least 30 trades and a t-statistic of 1.65 or more. Even then, past results don't guarantee future ones. "Avg/trade" is the return on the money at risk, after Kalshi's taker fee.</p>
        </Card>

        <Card title="Your underdog question" span={4}>
          {!studies ? <Empty>Needs history.</Empty> : (<>
            <Stat label="Underdog 25-40¢ in minutes 5-8 reached 50¢ before minute 12" value={pct(studies.underdog_bounce.reached_50c_rate)}
              sub={`${studies.underdog_bounce.n} windows · when it didn't, median bid at minute 12 was ${c(studies.underdog_bounce.median_bid_at_min12_when_no_flip)}`} />
          </>)}
        </Card>

        <Card title="Last-minute flips" span={4}>
          {!studies ? <Empty>Needs history.</Empty> : (
            <table><thead><tr><th>When</th><th>Favorite</th><th>Windows</th><th>Lost (price implied)</th></tr></thead>
              <tbody>{studies.late_flips.map((x: any, i: number) => (<tr key={i}><td>{x.when}</td><td>≥{x.favorite_at_least}</td><td>{x.n}</td>
                <td><b>{pct(x.favorite_lost_rate, 1)}</b> (≤{pct(x.implied_loss_rate)})</td></tr>))}</tbody></table>)}
          <p className="notice">If favorites lose more often than their price implies, late flips are real and you were right not to hold. Part of it is mechanical: settlement averages the last 60 seconds.</p>
        </Card>

        <Card title="Are the odds honest?" span={4} right={<span>minute <select value={minute} onChange={(e) => setMinute(Number(e.target.value))}>{[3, 5, 7, 9, 11, 13, 14].map((m) => <option key={m}>{m}</option>)}</select></span>}>
          {!studies ? <Empty>Needs history.</Empty> : (<>
            <table><thead><tr><th>UP priced</th><th>Windows</th><th>Avg price</th><th>Actually went UP</th></tr></thead>
              <tbody>{studies.calibration.filter((x: any) => x.minute === minute).map((x: any) => (<tr key={x.bucket}><td>{x.bucket}</td><td>{x.n}</td>
                <td>{c(x.avg_price)}</td><td><b>{pct(x.yes_rate)}</b></td></tr>))}</tbody></table>
            <p className="notice">{studies.model_vs_market.map((m: any) => `Minute ${m.minute}: BTC model score ${m.brier_model?.toFixed(3) ?? "—"} vs Kalshi ${m.brier_market?.toFixed(3) ?? "—"}`).join(" · ")} (lower is more accurate)</p>
          </>)}
        </Card>

        <Card title="History & paper track record" span={12}>
          <div className="stats">
            <Stat label="Windows with odds history" value={r?.coverage?.with_odds_history ?? "—"} sub={r?.coverage?.from ? `${fmtTime(r.coverage.from)} → ${fmtTime(r.coverage.to)}` : `backfilling ${r?.coverage?.backfill_days ?? 30} days`} />
            {Object.entries(r?.paper_track_record ?? {}).map(([k, v]: any) => (
              <Stat key={k} label={`${KIND[k]} (if followed exactly)`} value={`${v.wins}/${v.n} won`} sub={`${v.avg_pnl_cents.toFixed(1)}¢/contract avg · ${usd(v.usd_at_full_size)} at full size`} />))}
          </div>
          {past.length > 0 && (<table><thead><tr><th>When</th><th>Setup</th><th>Entry</th><th>Exit</th><th>Result</th></tr></thead>
            <tbody>{past.map((s: any) => (<tr key={s.id}><td>{fmtTime(s.created_at)}</td><td>{KIND[s.kind]} · {s.side}</td><td>{c(s.entry_price)} x{s.contracts}</td>
              <td>{c(s.exit_price)} <Badge v={s.status} /></td><td>{s.paper_pnl === null ? "—" : usd(Number(s.paper_pnl) * s.contracts)}</td></tr>))}</tbody></table>)}
        </Card>
      </div>
    </Shell>
  );
}
