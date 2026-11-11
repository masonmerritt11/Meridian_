"use client";
import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Sparkline } from "@/components/ui";
import { api, fmtTime, post, usd, usePoll } from "@/lib/api";

function CaseDetail({ id }: { id: string }) {
  const { data, error, reload } = usePoll<any>(`/cases/${id}`, 15000);
  const [candles, setCandles] = useState<any[]>([]);
  const [recon, setRecon] = useState<any | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [fill, setFill] = useState({ quantity: "", entry_price: "", stop_price: "", target_price: "" });
  const c = data?.case;
  const memo = data?.memos?.at(-1);
  useEffect(() => { if (c) api(`/marketdata/candles/${c.instrument_id}?limit=150`).then(setCandles).catch(() => {}); }, [c?.instrument_id]);
  useEffect(() => {
    const t = memo?.memo?.trade;
    if (t) setFill({ quantity: t.suggested_quantity ?? "", entry_price: t.entry_reference ?? "", stop_price: t.stop ?? "", target_price: t.target ?? "" });
  }, [memo?.id]);
  if (error) return <ErrorNote error={error} />;
  if (!c) return <Empty>Loading case…</Empty>;
  const h = c.hypothesis, m = memo?.memo, risk = data.risk_decisions.at(-1);
  const forEv = data.evidence.filter((e: any) => e.stance === "SUPPORTS");
  const against = data.evidence.filter((e: any) => e.stance === "OPPOSES");
  const other = data.evidence.filter((e: any) => !["SUPPORTS", "OPPOSES"].includes(e.stance));

  return (
    <div className="grid">
      <Card title={`Case · ${c.asset} ${c.direction}`} span={12} right={<span><Badge v={c.state} /> {c.data_source === "SIMULATED" && <Badge v="SIMULATED" />}</span>}>
        <div className="split">
          <div>
            <div style={{ marginBottom: 8 }}><b>Original hypothesis</b> <span className="notice">(immutable)</span><div>{h.statement}</div></div>
            <dl className="kv">
              <dt>Instrument</dt><dd>{c.instrument_id}</dd>
              <dt>Strategy</dt><dd>{h.strategy} v{h.strategy_version}</dd>
              <dt>Signal bar</dt><dd>{fmtTime(h.as_of)}</dd>
              <dt>Entry ref / stop / target</dt><dd>{h.entry_reference} / {h.stop} / {h.target}</dd>
              <dt>Strength</dt><dd>{h.strength}</dd>
              <dt>EMA fast / slow</dt><dd>{h.calculations?.ema_fast} / {h.calculations?.ema_slow}</dd>
              <dt>ATR / ATR%</dt><dd>{h.calculations?.atr} / {h.calculations?.atr_pct}%</dd>
              <dt>Breakout level</dt><dd>{c.direction === "LONG" ? h.calculations?.prior_high : h.calculations?.prior_low}</dd>
              <dt>Input window</dt><dd title={h.calculations?.window_hash}>{h.calculations?.window?.bars} bars · {h.calculations?.window_hash?.slice(0, 12)}…</dd>
              <dt>Revisions</dt><dd>{c.current_revision}</dd>
            </dl>
          </div>
          <Sparkline points={candles.map((k) => Number(k.close))} marks={[
            { y: Number(h.entry_reference), label: "entry", tone: "info" },
            { y: Number(h.stop), label: "stop", tone: "bad" },
            { y: Number(h.target), label: "target", tone: "ok" }]} />
        </div>
      </Card>

      <Card title="Committee memo" span={7}>
        {!m ? <Empty>No memo yet; the case is still in the pipeline.</Empty> : (
          <div className="memo">
            <div className="headline"><Badge v={memo.recommendation} /> {m.headline}</div>
            <div className="notice">{m.rationale} Score {m.score}.</div>
            {m.action_for_cio && <div className="action">{m.action_for_cio}</div>}
            <dl className="kv">
              <dt>Suggested size</dt><dd>{m.trade.suggested_quantity}</dd>
              <dt>Max loss at stop</dt><dd>{usd(m.trade.max_loss_usd)}</dd>
              <dt>Reward : risk</dt><dd>{m.trade.reward_risk ?? "—"}</dd>
              <dt>Notional</dt><dd>{usd(m.trade.notional_usd)}</dd>
            </dl>
            <h3>Evidence for</h3><ul className="list">{m.evidence_for.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
            <h3>Evidence against / dissent</h3>{m.dissent.length ? <ul className="list">{m.dissent.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul> : <div className="notice">No dissent recorded.</div>}
            <h3>Uncertainty</h3><ul className="list">{m.uncertainty.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
            <div className="notice" style={{ marginTop: 8 }}>{m.sizing_note} {m.disclaimer}</div>
            <div className="form-row" style={{ marginTop: 10 }}>
              <button onClick={async () => setRecon(await api(`/memos/${memo.id}/reconstruct`))}>Reconstruct from inputs</button>
              {recon && <span><Badge v={recon.reconstructed ? "MATCH" : "MISMATCH"} title="reconstruction" /> {recon.checks.filter((x: any) => x.match).length}/{recon.checks.length} layers match</span>}
            </div>
            {recon && <table style={{ marginTop: 6 }}><tbody>{recon.checks.map((x: any) => <tr key={x.layer}><td className="mono">{x.layer}</td><td><Badge v={x.match ? "MATCH" : "MISMATCH"} /></td></tr>)}</tbody></table>}
          </div>)}
      </Card>

      <Card title="CIO decision" span={5}>
        {!memo ? <Empty>Available once the committee has issued a memo.</Empty> : (<>
          <div className="form-row">
            <label style={{ flex: 1 }}>Note<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
            {(["ACCEPT", "DEFER", "DECLINE"] as const).map((d) => (
              <button key={d} className={d === "ACCEPT" ? "primary" : d === "DECLINE" ? "danger" : ""} disabled={d === "ACCEPT" && c.state !== "RECOMMENDED"}
                onClick={async () => { try { await post(`/cases/${id}/decision`, { decision: d, note }); setMsg(`Recorded ${d}`); reload(); } catch (e: any) { setMsg(e.message); } }}>{d}</button>))}
          </div>
          {c.state !== "RECOMMENDED" && <div className="notice">Accept is only available for RECOMMENDED cases. Risk vetoes are final.</div>}
          {data.cio_decisions.map((d: any) => <div key={d.id} className="notice"><Badge v={d.decision} /> {d.username} · {fmtTime(d.created_at)} {d.note}</div>)}

          <h3>Record the trade you executed</h3>
          <div className="notice">Enter your actual fills. MERIDIAN then monitors it and tells you HOLD or EXIT.</div>
          <form className="form-row" style={{ marginTop: 6 }} onSubmit={async (e) => {
            e.preventDefault();
            try { await post("/positions", { case_id: id, instrument_id: c.instrument_id, direction: c.direction, ...fill, target_price: fill.target_price || null }); setMsg("Position recorded; other open recommendations re-sent to risk."); reload(); }
            catch (x: any) { setMsg(x.message); }
          }}>
            {(["quantity", "entry_price", "stop_price", "target_price"] as const).map((k) => (
              <label key={k}>{k.replace("_", " ")}<input style={{ width: 110 }} value={fill[k]} onChange={(e) => setFill({ ...fill, [k]: e.target.value })} required={k !== "target_price"} inputMode="decimal" /></label>))}
            <button className="primary">Record position</button>
          </form>
          {msg && <div className="notice" style={{ marginTop: 6 }}>{msg}</div>}
          {data.positions.map((p: any) => <div key={p.id} className="notice" style={{ marginTop: 6 }}><Badge v={p.status} /> {p.quantity} @ {p.entry_price} stop {p.stop_price} · advice <Badge v={p.advisory?.action ?? "PENDING"} /></div>)}
        </>)}
      </Card>

      <Card title="Risk engine checks" span={7} right={risk && <span><Badge v={risk.outcome} /> config v{risk.config_version}</span>}>
        {!risk ? <Empty>Not yet reviewed by risk.</Empty> : (
          <table><thead><tr><th>Check</th><th>Result</th><th>Code</th><th>Detail</th></tr></thead>
            <tbody>{risk.checks.map((k: any, i: number) => (
              <tr key={i}><td className="mono">{k.check}</td><td><Badge v={k.outcome} /></td><td className="mono">{k.code}</td><td>{k.detail}</td></tr>))}</tbody></table>)}
      </Card>

      <Card title="Evidence" span={5}>
        {[["Supports", forEv], ["Opposes", against], ["Context / unavailable", other]].map(([label, list]: any) => (
          <div key={label}><h3>{label}</h3>{!list.length ? <div className="notice">None.</div> : list.map((e: any) => (
            <div key={e.id} style={{ marginBottom: 6 }}><Badge v={e.stance} /> <span className="code">{e.kind}</span> {e.summary}<div className="notice mono">{e.producer}</div></div>))}</div>))}
      </Card>

      <Card title="Lifecycle" span={6}>
        <ul className="timeline">{data.transitions.map((t: any, i: number) => (
          <li key={i}><div><b>{t.to_state}</b> <span className="notice">{t.reason}</span></div><div className="t">{fmtTime(t.created_at)} · {t.actor}</div></li>))}</ul>
      </Card>
      <Card title="Event trace (correlation)" span={6}>
        <table><tbody>{data.events.map((e: any) => (
          <tr key={e.id}><td>{fmtTime(e.occurred_at)}</td><td className="mono">{e.event_type}</td><td className="mono notice">{e.producer}</td></tr>))}</tbody></table>
      </Card>
    </div>
  );
}

export default function Lab() {
  const [id, setId] = useState<string | null>(null);
  useEffect(() => { setId(new URLSearchParams(location.search).get("id")); }, []);
  const cases = usePoll<any[]>("/cases?limit=100", 15000);
  return (
    <Shell title="Research Laboratory">
      {id ? (<>
        <div style={{ marginBottom: 12 }}><button className="link" onClick={() => { history.pushState(null, "", "/lab/"); setId(null); }}>← All cases</button></div>
        <CaseDetail id={id} />
      </>) : (
        <Card title="Research cases">
          <ErrorNote error={cases.error} />
          {!cases.data?.length ? <Empty>No research cases yet.</Empty> : (
            <table><thead><tr><th>Opened</th><th>Case</th><th>State</th><th>Entry / Stop / Target</th><th>Strength</th><th>Risk</th><th>Committee</th><th>Source</th></tr></thead>
              <tbody>{cases.data.map((c) => (
                <tr key={c.id} className="click" onClick={() => { history.pushState(null, "", `/lab/?id=${c.id}`); setId(c.id); }}>
                  <td>{fmtTime(c.created_at)}</td><td><Badge v={c.direction} /> {c.asset}</td><td><Badge v={c.state} /></td>
                  <td className="num">{c.entry} / {c.stop} / {c.target}</td><td className="num">{c.strength}</td>
                  <td><Badge v={c.risk_outcome} /></td><td><Badge v={c.recommendation} /></td><td><Badge v={c.data_source} /></td>
                </tr>))}</tbody></table>)}
        </Card>)}
    </Shell>
  );
}
