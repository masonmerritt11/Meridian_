"use client";
import Link from "next/link";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Meter, Stat } from "@/components/ui";
import { ago, fmtTime, post, usd, usePoll } from "@/lib/api";

export default function Overview() {
  const { data, error, reload } = usePoll<any>("/overview", 10000);
  const plan = usePoll<any>("/portfolio/plan", 15000);
  const pt = plan.data?.totals;
  const [running, setRunning] = useState(false);
  const [demoMsg, setDemoMsg] = useState<string | null>(null);
  const pf = data?.portfolio;
  const eq = Number(pf?.equity ?? 0);
  const openRisk = Number(pf?.open_risk_usd ?? 0);
  const recs = data?.case_counts?.RECOMMENDED ?? 0;
  const health = data?.worker_health ?? {};
  const unhealthy = (health.FAILING ?? 0) + (health.STALE ?? 0);
  const exits = (data?.open_positions ?? []).filter((p: any) => p.advisory?.action === "EXIT").length;

  return (
    <Shell title="Executive Overview">
      <ErrorNote error={error} />
      <div className="stats">
        <Stat label="Equity (CIO-recorded)" value={usd(pf?.equity)} sub={`base ${usd(pf?.capital_base)} · realized ${usd(pf?.realized_total)}`} />
        <Stat label="Open risk to stops" value={usd(pf?.open_risk_usd)} tone={openRisk > eq * 0.1 ? "bad" : undefined}
          sub={<><Meter value={openRisk} max={eq * 0.1} /> limit {usd(eq * 0.1)} (10%)</>} />
        <Stat label="Active recommendations" value={recs} tone={recs ? "ok" : undefined} sub={`${data?.case_counts?.BLOCKED ?? 0} blocked · ${data?.case_counts?.DEFERRED ?? 0} deferred`} />
        <Stat label="Exit advisories" value={exits} tone={exits ? "bad" : undefined} sub={`${data?.open_positions?.length ?? 0} open position(s)`} />
        <Stat label="Worker health" value={`${health.HEALTHY ?? 0}/${data?.workers_implemented ?? 0}`} tone={unhealthy ? "bad" : undefined}
          sub={`${data?.workers_total ?? 0} roles defined · DLQ ${data?.dlq_depth ?? 0}`} />
      </div>

      <div className="grid">
        <Card title="Allocation plan: today's best ideas within your limits" span={12}
          right={pt && <span className="notice">capital {usd(Number(pt.open_capital_usd) + Number(pt.planned_capital_usd))} of {usd(pt.capital_limit_usd)} · idle {usd(pt.idle_capital_usd)} · new risk {usd(pt.planned_risk_usd)}</span>}>
          {pt && <div style={{ marginBottom: 8 }}><Meter value={Number(pt.open_capital_usd) + Number(pt.planned_capital_usd)} max={Number(pt.capital_limit_usd)} /></div>}
          {!plan.data?.allocations?.length ? <Empty>No allocation right now: no live recommendation fits your limits. {plan.data?.idle_note}</Empty> : (
            <table><thead><tr><th>#</th><th>Position</th><th>Contracts</th><th>Entry / Stop / Target</th><th>Risk at stop</th><th>Capital</th><th>Score</th><th>R:R</th><th>Notes</th><th /></tr></thead>
              <tbody>{plan.data.allocations.map((a: any) => (
                <tr key={a.case_id}>
                  <td className="num">{a.rank}</td>
                  <td><Badge v={a.direction} /> {a.asset} <span className="notice mono">{a.instrument_id}</span></td>
                  <td className="num">{a.quantity}{a.reduced_from && <span className="notice"> (of {a.reduced_from})</span>}</td>
                  <td className="num">{a.entry} / {a.stop} / {a.target}</td>
                  <td className="num">{usd(a.risk_usd)}</td>
                  <td className="num">{a.capital_usd ? usd(a.capital_usd) : <Badge v="UNKNOWN" title="margin unverified" />}</td>
                  <td className="num">{Number(a.score).toFixed(1)}</td><td className="num">{a.reward_risk ?? "—"}</td>
                  <td className="notice">{a.notes.join("; ")}</td>
                  <td><Link href={`/lab/?id=${a.case_id}`}>Open</Link></td>
                </tr>))}</tbody></table>)}
          {!!plan.data?.skipped?.length && (
            <div className="notice" style={{ marginTop: 8 }}>Not allocated: {plan.data.skipped.map((k: any) => `${k.asset} ${k.direction} (${k.reason.replace("_", " ").toLowerCase()})`).join(" · ")}</div>)}
          <div className="notice" style={{ marginTop: 6 }}>Ranked by committee score, then reward:risk. This is a ranking heuristic, not a probability of profit.</div>
        </Card>

        <Card title="Latest committee memos" span={7} right={<Link href="/archive/">Archive →</Link>}>
          {!data?.latest_memos?.length ? <Empty>No memos yet. They appear when a signal clears research, risk and committee.</Empty> : (
            <table><thead><tr><th>When</th><th>Recommendation</th><th>Instrument</th><th>Entry / Stop / Target</th><th>Qty</th><th>Max loss</th><th>Risk</th><th /></tr></thead>
              <tbody>{data.latest_memos.map((m: any) => (
                <tr key={m.id}>
                  <td>{fmtTime(m.created_at)}</td>
                  <td><Badge v={m.recommendation} /> {m.data_source === "SIMULATED" && <Badge v="SIMULATED" />}</td>
                  <td><Badge v={m.trade?.direction} /> {m.trade?.instrument_id}</td>
                  <td className="num">{m.trade?.entry_reference} / {m.trade?.stop} / {m.trade?.target}</td>
                  <td className="num">{m.trade?.suggested_quantity}</td>
                  <td className="num">{usd(m.trade?.max_loss_usd)}</td>
                  <td><Badge v={m.risk?.outcome} /></td>
                  <td><Link href={`/lab/?id=${m.case_id}`}>Open</Link></td>
                </tr>))}</tbody></table>)}
        </Card>

        <Card title="Open positions — hold or exit" span={5} right={<Link href="/risk/">Risk →</Link>}>
          {!data?.open_positions?.length ? <Empty>No open positions recorded. Record trades you execute in the Research Laboratory or Risk Command.</Empty> : (
            <table><thead><tr><th>Position</th><th>Advice</th><th>Why</th></tr></thead>
              <tbody>{data.open_positions.map((p: any) => (
                <tr key={p.id}>
                  <td><Badge v={p.direction} /> {p.quantity} × {p.instrument_id}<div className="notice">entry {p.entry_price} · stop {p.stop_price}</div></td>
                  <td><Badge v={p.advisory?.action ?? "PENDING"} /></td>
                  <td><div className="codes">{(p.advisory?.reason_codes ?? []).map((c: string) => <span className="code" key={c}>{c}</span>)}</div>
                    <div className="notice">{p.advisory ? ago(p.advisory.created_at) : "awaiting first evaluation"}</div></td>
                </tr>))}</tbody></table>)}
        </Card>

        <Card title="Alerts" span={7}>
          {!data?.alerts?.length ? <Empty>No active alerts.</Empty> : (
            <table><tbody>{data.alerts.map((a: any, i: number) => (
              <tr key={i}><td style={{ width: 120 }}>{fmtTime(a.ts)}</td><td><span className={`dot ${a.severity === "high" ? "bad" : "warn"}`} />{a.title}</td>
                <td><div className="codes">{a.codes?.map((c: string) => <span className="code" key={c}>{c}</span>)}</div></td></tr>))}</tbody></table>)}
        </Card>

        <Card title="Market-data freshness" span={5} right={<span className="notice">{data?.data_sources?.join(", ")}</span>}>
          {!data?.freshness?.length ? <Empty>No datasets ingested yet.</Empty> : (
            <table><thead><tr><th>Dataset</th><th>Status</th><th>Last data</th></tr></thead>
              <tbody>{data.freshness.filter((f: any) => !f.dataset.startsWith("simulated:")).map((f: any) => (
                <tr key={f.dataset} title={f.detail}><td className="mono">{f.dataset}</td><td><Badge v={f.status} /></td><td>{ago(f.last_data_ts)}</td></tr>))}</tbody></table>)}
        </Card>

        {data?.mode === "simulated" && (
          <Card title="Demonstration" span={12}>
            <div className="form-row">
              <button className="primary" disabled={running} onClick={async () => {
                setRunning(true); setDemoMsg(null);
                try { const r = await post<any>("/demo/run", {}); setDemoMsg(`${r.memos.length} memo(s); ${r.events_handled} events processed. ${r.label}`); reload(); }
                catch (e: any) { setDemoMsg(e.message); } finally { setRunning(false); }
              }}>{running ? "Running pipeline…" : "Run end-to-end pipeline on simulated data"}</button>
              <span className="notice">{demoMsg ?? "Runs discovery, ingest, trend, case intake, macro, challenge, risk and committee once."}</span>
            </div>
          </Card>
        )}
      </div>
    </Shell>
  );
}
