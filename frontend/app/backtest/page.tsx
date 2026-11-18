"use client";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Sparkline, Stat } from "@/components/ui";
import { fmtTime, post, usd, usePoll } from "@/lib/api";

const R = (v: any, d = 2) => (v === null || v === undefined ? "—" : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(d)}R`);
const P = (v: any) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);
const D = (r: any) => (r === null || r === undefined ? "—" : `${Number(r) >= 0 ? "+" : "−"}${usd(Math.abs(Number(r) * 70))}`);

function TwoLines({ a, b }: { a: any[]; b: any[] }) {
  if ((a?.length ?? 0) < 2 && (b?.length ?? 0) < 2) return <Empty>Not enough trades to chart.</Empty>;
  const W = 520, H = 130, pad = 6;
  const all = [...(a ?? []), ...(b ?? [])];
  const ts = all.map((p) => new Date(p.t).getTime()), rs = all.map((p) => p.r).concat([0]);
  const t0 = Math.min(...ts), t1 = Math.max(...ts) || t0 + 1, lo = Math.min(...rs), hi = Math.max(...rs);
  const x = (t: string) => pad + ((new Date(t).getTime() - t0) / Math.max(t1 - t0, 1)) * (W - 2 * pad);
  const y = (r: number) => H - pad - ((r - lo) / Math.max(hi - lo, 0.001)) * (H - 2 * pad);
  const path = (pts: any[]) => pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.r).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="cumulative R, current vs proposed">
      <line x1={pad} x2={W - pad} y1={y(0)} y2={y(0)} stroke="currentColor" strokeOpacity="0.2" strokeDasharray="4 4" />
      {a?.length > 1 && <path d={path(a)} fill="none" stroke="#8a8f98" strokeWidth="1.8" />}
      {b?.length > 1 && <path d={path(b)} fill="none" stroke="#4cc38a" strokeWidth="2" />}
    </svg>
  );
}

function Compare({ label, base, vari }: { label: string; base: any; vari: any }) {
  const row = (k: string, f: (v: any) => string, better: "hi" | "lo" = "hi") => {
    const good = better === "hi" ? Number(vari[k]) > Number(base[k]) : Number(vari[k]) < Number(base[k]);
    return <td className="num"><span className="notice">{f(base[k])} →</span> <b style={{ color: good ? "var(--ok)" : undefined }}>{f(vari[k])}</b></td>;
  };
  return (
    <tr><td>{label}</td><td className="num"><span className="notice">{base.n} →</span> <b>{vari.n}</b></td>
      {row("win_rate", P)}{row("avg_r", (v) => R(v))}{row("total_r", (v) => `${R(v, 1)} (${D(v)})`)}{row("max_dd_r", (v) => R(-Number(v), 1), "lo")}</tr>
  );
}

function ProposalCard({ p, reload }: { p: any; reload: () => void }) {
  const ev = p.evidence ?? {};
  const [err, setErr] = useState<string | null>(null);
  const decide = async (status: string) => {
    setErr(null);
    try {
      if (status === "ACCEPTED") {
        const password = window.prompt(`Apply "${p.title}" to the live strategy? Enter your password:`);
        if (!password) return;
        await post(`/backtest/proposals/${p.id}/decision`, { status, password });
      } else {
        const note = window.prompt("Why reject it? (optional)") ?? "";
        await post(`/backtest/proposals/${p.id}/decision`, { status, note });
      }
      reload();
    } catch (e: any) { setErr(e.message); }
  };
  const revert = async () => {
    const password = window.prompt("Revert this change? Enter your password:");
    if (!password) return;
    try { await post(`/backtest/proposals/${p.id}/revert`, { password }); reload(); } catch (e: any) { setErr(e.message); }
  };
  return (
    <Card title={p.title} span={12} right={<span><Badge v={p.status} /> <span className="notice">updated {fmtTime(p.updated_at)}</span></span>}>
      {ev.base_test ? (<>
        <table><thead><tr><th /><th>Trades</th><th>Win rate</th><th>Avg per trade</th><th>Total ($ at $70/R)</th><th>Worst losing run</th></tr></thead>
          <tbody>
            <Compare label="Older data (tuned on)" base={ev.base_train} vari={ev.var_train} />
            <Compare label="Newest data (never seen)" base={ev.base_test} vari={ev.var_test} />
          </tbody></table>
        <div className="form-row" style={{ alignItems: "center" }}>
          <div style={{ flex: 1, minWidth: 320 }}><TwoLines a={ev.base_curve} b={ev.var_curve} />
            <div className="notice"><span style={{ color: "#8a8f98" }}>━</span> current strategy &nbsp; <span style={{ color: "#4cc38a" }}>━</span> with this change, on the newest data</div></div>
          <div style={{ width: 280 }}>
            <Stat label="Chance it's a real improvement" value={P(ev.prob)} sub={`bootstrap over ${ev.months} months of never-seen data`} tone={Number(ev.prob) >= 0.9 ? "ok" : "warn"} />
          </div>
        </div>
      </>) : <Empty>No evidence attached.</Empty>}
      {p.note && <p className="notice">{p.note}</p>}
      <ErrorNote error={err} />
      <div className="form-row">
        {p.status === "PROPOSED" && <><button className="primary" onClick={() => decide("ACCEPTED")}>Accept and apply</button><button onClick={() => decide("REJECTED")}>Reject</button></>}
        {p.status === "ACCEPTED" && <button className="danger" onClick={revert}>Revert</button>}
        <span className="notice">{p.status === "PROPOSED" ? "Accepting changes the live strategy from the next signal on. It's versioned and can be reverted." : p.decided_by ? `${p.status.toLowerCase()} by ${p.decided_by} ${fmtTime(p.decided_at)}` : ""}</span>
      </div>
    </Card>
  );
}

function Proposals({ d, reload }: { d: any; reload: () => void }) {
  const open = d.proposals.filter((p: any) => p.status === "PROPOSED");
  const past = d.proposals.filter((p: any) => p.status !== "PROPOSED");
  return (
    <div className="grid">
      {!open.length && <Card title="No proven improvements right now" span={12}>
        <p>Every few hours the lab replays the live strategy on years of hourly charts and tries {d.run?.tested ?? "dozens of"} small changes. A change only appears here if it beat the current strategy on the older data <b>and</b> on the newest data it was never tuned on, with enough trades and at least a 90% chance that the improvement isn't luck.</p>
        <p className="notice">{d.run ? `Last run ${fmtTime(d.run.created_at)}: ${d.run.tested} changes tested, ${d.run.passed} passed.` : "The first run starts once the history has loaded."}</p>
      </Card>}
      {open.map((p: any) => <ProposalCard key={p.id} p={p} reload={reload} />)}
      {past.length > 0 && <Card title="Past proposals" span={12}>
        <table><thead><tr><th>Change</th><th>Status</th><th>Newest-data result</th><th>When</th><th /></tr></thead>
          <tbody>{past.map((p: any) => (<tr key={p.id} title={p.note}><td>{p.title}</td><td><Badge v={p.status} /></td>
            <td className="num">{p.evidence?.var_test ? `${R(p.evidence.base_test.total_r, 1)} → ${R(p.evidence.var_test.total_r, 1)}` : "—"}</td>
            <td>{fmtTime(p.decided_at ?? p.updated_at)}</td><td /></tr>))}</tbody></table>
        {past.filter((p: any) => p.status === "ACCEPTED").map((p: any) => <div key={p.id} style={{ marginTop: 10 }}><ProposalCard p={p} reload={reload} /></div>)}
      </Card>}
    </div>
  );
}

function StrategyBacktest({ d }: { d: any }) {
  const run = d.run;
  if (!run) return <div className="grid"><Card title="Strategy backtest" span={12}><Empty>{d.bars_stored ? `${d.bars_stored.toLocaleString()} hourly bars loaded; the first run is coming.` : "Loading history (up to two years of hourly charts per asset)…"}</Empty></Card></div>;
  const b = run.baseline;
  const curve = (b.curve ?? []).map((p: any) => p.r);
  return (
    <div className="grid">
      <Card title="The live strategy on past charts" span={12} right={<span className="notice">run {fmtTime(run.created_at)} · strategy version {run.config_version}</span>}>
        <div className="stats">
          <Stat label="Trades" value={b.all.n} sub={`${fmtTime(run.start)} → ${fmtTime(run.end)}`} />
          <Stat label="Win rate" value={P(b.all.win_rate)} sub={`${b.all.targets} targets · ${b.all.stops} stops · ${b.all.time_exits} timed out`} />
          <Stat label="Average per trade" value={R(b.all.avg_r)} sub={`${D(b.all.avg_r)} at $70 risk`} tone={Number(b.all.avg_r) > 0 ? "ok" : "bad"} />
          <Stat label="Total" value={R(b.all.total_r, 1)} sub={D(b.all.total_r)} tone={Number(b.all.total_r) > 0 ? "ok" : "bad"} />
          <Stat label="Worst losing run" value={R(-b.all.max_dd_r, 1)} sub={D(-b.all.max_dd_r)} />
          <Stat label="Profit factor" value={b.all.profit_factor ? Number(b.all.profit_factor).toFixed(2) : "—"} sub="gross wins ÷ gross losses" />
        </div>
        {curve.length > 1 ? <Sparkline points={curve} height={170} /> : <Empty>No completed trades.</Empty>}
        <p className="notice">Cumulative result in R (1R = the $70 at risk). Entry at the next hour's open, the live stop and target, one position per asset, 0.1 ATR per trade for slippage and fees. If a bar touches both stop and target, the stop is counted.</p>
      </Card>
      <Card title="By asset" span={6}>
        <table><thead><tr><th>Asset</th><th>Trades</th><th>Win rate</th><th>Avg</th><th>Total</th></tr></thead>
          <tbody>{Object.entries(b.by_asset).map(([a, s]: any) => (<tr key={a}><td>{a}</td><td>{s.n}</td><td>{P(s.win_rate)}</td>
            <td className="num" style={{ color: s.avg_r >= 0 ? "var(--ok)" : "var(--bad)" }}>{R(s.avg_r)}</td><td className="num">{R(s.total_r, 1)} ({D(s.total_r)})</td></tr>))}</tbody></table>
      </Card>
      <Card title="History used" span={6}>
        <table><thead><tr><th>Asset</th><th>Market</th><th>Hours</th><th>From</th><th>To</th></tr></thead>
          <tbody>{run.coverage.map((c: any) => (<tr key={c.product}><td>{c.asset}</td><td>{c.product}</td><td>{c.bars.toLocaleString()}</td><td>{fmtTime(c.from)}</td><td>{fmtTime(c.to)}</td></tr>))}</tbody></table>
        <p className="notice">BTC, ETH and XRP use Coinbase spot; gold uses PAXG (backed by physical gold). Silver and oil test each futures contract on its own chart so contract rolls never create fake breakouts.</p>
      </Card>
      <Card title={`Every change tested (${run.tested})`} span={12}>
        <table><thead><tr><th>Change</th><th>Newest data: trades</th><th>Avg</th><th>Total vs current</th><th>Chance real</th><th>Verdict</th></tr></thead>
          <tbody>{run.variants.map((v: any) => (<tr key={v.key}><td>{v.title}</td><td>{v.var_test.n}</td><td className="num">{R(v.var_test.avg_r)}</td>
            <td className="num">{R(v.var_test.total_r - v.base_test.total_r, 1)}</td><td>{P(v.prob)}</td>
            <td>{v.passed ? <Badge v="PROVEN" /> : <span className="notice">{(v.reasons ?? [])[0]}</span>}</td></tr>))}</tbody></table>
      </Card>
    </div>
  );
}

const SM = ["0.5", "0.75", "1.0", "1.5", "2.0"], TM = ["0.5", "1.0", "1.5", "2.0", "3.0", "none"];

function RecResults() {
  const { data: d, error } = usePoll<any>("/backtest/recommendations", 60000);
  if (!d) return <ErrorNote error={error} />;
  const g = d.groups.RECOMMEND, dg = d.diagnostics;
  const counter = ["BLOCKED", "REJECT", "DEFER"].map((k) => [k, d.groups[k]] as const).filter(([, v]) => v.total > 0);
  const cell = (k: string) => d.alternatives[k];
  const cur = "s1.0_t1.5";
  return (
    <div className="grid">
      <Card title="Every recommendation, tested as if you placed it" span={12}>
        <div className="stats">
          <Stat label="Recommendations" value={g.total} sub={`${g.filled} reached the entry · ${g.not_filled} never did · ${g.pending} too new`} />
          <Stat label="Winners / losers" value={`${g.winners} / ${g.losers}`} sub={`${g.targets} hit target · ${g.stops} hit stop · ${g.open} still open`} />
          <Stat label="Win rate" value={P(g.win_rate)} />
          <Stat label="Average per trade" value={R(g.avg_r)} tone={g.avg_r === null ? undefined : Number(g.avg_r) > 0 ? "ok" : "bad"} sub={D(g.avg_r)} />
          <Stat label="Total if all were taken" value={R(g.total_r, 1)} sub={usd(g.usd_at_70)} tone={Number(g.total_r) > 0 ? "ok" : Number(g.total_r) < 0 ? "bad" : undefined} />
        </div>
        <p className="notice">Filled if price traded at the entry within 12 hours of the recommendation. Then the recommended stop and target apply, with the stop counted first if one hour touched both. Results are in R; 1R = $70.</p>
      </Card>
      <Card title="Is the stop or target hurting us?" span={6}>
        <ul className="list">
          <li><b>{dg.stopped_then_target}</b> of {dg.stopped} stopped-out trades went on to reach the original target within 5 days{dg.stopped ? ` (${P(dg.stopped_then_target / dg.stopped)})` : ""}. A high share means the stop is too tight.</li>
          <li>Losing trades were up <b>{R(dg.losers_avg_best_r_before_stop)}</b> on average before they turned and hit the stop. Above about 1R, moving the stop to breakeven would have saved money.</li>
          <li>After hitting the target, winners kept running another <b>{R(dg.after_target_avg_extra_r)}</b> on average. A large number means the target is too close.</li>
          <li>Breakeven rule (stop moves to entry at +1R): <b>{R(cell("breakeven_at_1r")?.avg_r)}</b> per trade vs <b>{R(cell(cur)?.avg_r)}</b> now ({cell("breakeven_at_1r")?.n ?? 0} trades).</li>
        </ul>
        {counter.length > 0 && <>
          <h4>Ideas the system turned down</h4>
          <table><thead><tr><th>Decision</th><th>Ideas</th><th>Win rate</th><th>Avg</th><th>Total</th></tr></thead>
            <tbody>{counter.map(([k, v]) => (<tr key={k}><td>{k === "BLOCKED" ? "Blocked by risk" : k === "REJECT" ? "Rejected by committee" : "Deferred"}</td><td>{v.total}</td>
              <td>{P(v.win_rate)}</td><td className="num">{R(v.avg_r)}</td><td className="num">{R(v.total_r, 1)}</td></tr>))}</tbody></table>
          <p className="notice">If turned-down ideas would have made money, the filters may be too strict; if they would have lost, the filters are earning their keep.</p>
        </>}
      </Card>
      <Card title="Same trades, different stop and target" span={6}>
        <table><thead><tr><th>Stop ↓ / Target →</th>{TM.map((t) => <th key={t}>{t === "none" ? "no target (2 days)" : `${t}R`}</th>)}</tr></thead>
          <tbody>{SM.map((s) => (<tr key={s}><td>{s}× stop</td>{TM.map((t) => {
            const c = cell(`s${s}_t${t}`); const v = c?.avg_r;
            return <td key={t} className="num" style={{ outline: `s${s}_t${t}` === cur ? "1px solid var(--accent)" : undefined, color: v === null || v === undefined ? undefined : v > 0 ? "var(--ok)" : "var(--bad)" }}
              title={`${c?.n ?? 0} trades`}>{v === null || v === undefined ? "—" : R(v)}</td>;
          })}</tr>))}</tbody></table>
        <p className="notice">Average result per trade, in R of each version's own risk, so every cell is sized to the same $70. The outlined cell is today's setting (stop 2×ATR, target 3×ATR = 1.5R). Small samples mislead: the Strategy Lab tab tests the same ideas on years of data.</p>
      </Card>
      <Card title="Each recommendation" span={12}>
        {!d.rows.length ? <Empty>No recommendations yet.</Empty> : (
          <table><thead><tr><th>When</th><th>Decision</th><th>Trade</th><th>Entry / Stop / Target</th><th>Result</th><th>Best / worst along the way</th><th /></tr></thead>
            <tbody>{d.rows.map((r: any) => (<tr key={r.memo_id} title={r.note ?? ""}>
              <td>{fmtTime(r.made_at)}</td><td><Badge v={r.decision} /></td>
              <td><Badge v={r.direction} /> {r.asset}</td><td className="num">{Number(r.entry)} / {Number(r.stop)} / {Number(r.target)}</td>
              <td>{r.status !== "FILLED" ? <span className="notice">{r.status === "NOT_FILLED" ? "never reached entry" : "waiting"}</span>
                : <><Badge v={r.outcome} /> <b className="num" style={{ color: Number(r.r) > 0 ? "var(--ok)" : Number(r.r) < 0 ? "var(--bad)" : undefined }}>{r.r === null ? "" : `${R(r.r)} ${D(r.r)}`}</b></>}</td>
              <td className="num">{r.mfe_r === null || r.mfe_r === undefined ? "—" : `${R(r.mfe_r)} / ${R(-r.mae_r)}`} {r.stopped_then_target && <span className="notice">· later hit target</span>}</td>
              <td><a href={`/lab/?id=${r.case_id}`}>Open</a></td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}

export default function Backtest() {
  const ov = usePoll<any>("/backtest/overview", 60000);
  const [tab, setTab] = useState("proposals");
  const open = (ov.data?.proposals ?? []).filter((p: any) => p.status === "PROPOSED").length;
  const T = (id: string, label: string) => <button className={tab === id ? "primary" : ""} onClick={() => setTab(id)}>{label}</button>;
  return (
    <Shell title="Backtest Lab">
      <ErrorNote error={ov.error} />
      <div className="form-row" style={{ marginBottom: 8 }}>{T("proposals", `Proposals${open ? ` (${open})` : ""}`)}{T("strategy", "Strategy backtest")}{T("recs", "Recommendation results")}</div>
      {tab === "recs" ? <RecResults /> : !ov.data ? null : tab === "proposals" ? <Proposals d={ov.data} reload={ov.reload} /> : <StrategyBacktest d={ov.data} />}
    </Shell>
  );
}
