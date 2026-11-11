"use client";
import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Meter, Stat } from "@/components/ui";
import { fmtTime, post, usd, usePoll } from "@/lib/api";

function PositionRow({ p, onDone }: { p: any; onDone: () => void }) {
  const [mode, setMode] = useState<"" | "close" | "stop">("");
  const [val, setVal] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <>
      <tr>
        <td><Badge v={p.direction} /> {p.quantity} × <span className="mono">{p.instrument_id}</span><div className="notice">opened {fmtTime(p.opened_at)}</div></td>
        <td className="num">{p.entry_price}</td><td className="num">{p.stop_price}</td><td className="num">{p.target_price ?? "—"}</td>
        <td className="num">{usd(p.risk_usd)}</td>
        <td className="num">{p.advisory?.detail?.last_price ?? "—"}<div className="notice">{usd(p.advisory?.detail?.unrealized_pnl_usd)}</div></td>
        <td><Badge v={p.advisory?.action ?? "PENDING"} /><div className="codes" style={{ marginTop: 4 }}>{(p.advisory?.reason_codes ?? []).map((c: string) => <span className="code" key={c}>{c}</span>)}</div>
          {p.advisory?.detail?.note && <div className="notice">{p.advisory.detail.note}</div>}</td>
        <td style={{ whiteSpace: "nowrap" }}><button onClick={() => setMode(mode === "stop" ? "" : "stop")}>Stop moved</button> <button className="danger" onClick={() => setMode(mode === "close" ? "" : "close")}>I exited</button></td>
      </tr>
      {mode && (
        <tr><td colSpan={8}>
          <form className="form-row" onSubmit={async (e) => {
            e.preventDefault(); setErr(null);
            try {
              if (mode === "close") await post(`/positions/${p.id}/close`, { exit_price: val, note: "recorded from Risk Command" });
              else await post(`/positions/${p.id}/stop`, { stop_price: val });
              setMode(""); onDone();
            } catch (x: any) { setErr(x.message); }
          }}>
            <label>{mode === "close" ? "Your fill price on exit" : "New stop you set at your broker"}<input value={val} onChange={(e) => setVal(e.target.value)} required inputMode="decimal" /></label>
            <button className="primary">Record</button>
            <span className="notice">Records what you did outside MERIDIAN. Nothing is sent to any exchange.</span>
            {err && <span className="error">{err}</span>}
          </form>
        </td></tr>)}
    </>
  );
}

export default function RiskCommand() {
  const pf = usePoll<any>("/portfolio", 15000);
  const pos = usePoll<any[]>("/positions", 10000);
  const alerts = usePoll<any[]>("/risk/alerts", 15000);
  const decisions = usePoll<any[]>("/risk/decisions?limit=40", 15000);
  const cfg = usePoll<any>("/risk/config", 60000);
  const active = cfg.data?.versions?.find((v: any) => v.status === "ACTIVE");
  const [draft, setDraft] = useState("");
  const [reason, setReason] = useState("");
  const [pw, setPw] = useState("");
  const [cfgMsg, setCfgMsg] = useState<string | null>(null);
  useEffect(() => { if (active && !draft) setDraft(JSON.stringify(active.config, null, 2)); }, [active, draft]);

  const eq = Number(pf.data?.equity ?? 0);
  const lossPct = Number(active?.config?.max_loss_per_trade_pct ?? 0.1);
  const openRisk = Number(pf.data?.open_risk_usd ?? 0);
  const totalCap = eq * Number(active?.config?.max_total_open_risk_pct ?? 0.1);
  const open = (pos.data ?? []).filter((p) => p.status === "OPEN");
  const closed = (pos.data ?? []).filter((p) => p.status === "CLOSED");

  return (
    <Shell title="Risk Command">
      <ErrorNote error={pf.error || pos.error} />
      <div className="stats">
        <Stat label="Equity" value={usd(pf.data?.equity)} sub={`capital base ${usd(pf.data?.capital_base)}`} />
        <Stat label="Max loss per trade" value={usd(eq * lossPct)} sub={`${(lossPct * 100).toFixed(0)}% of equity — stop distance × size`} />
        <Stat label="Open risk / cap" value={`${usd(openRisk)} / ${usd(totalCap)}`} tone={openRisk >= totalCap && totalCap > 0 ? "bad" : undefined} sub={<Meter value={openRisk} max={totalCap} />} />
        <Stat label="Realized today / week" value={`${usd(pf.data?.realized_today)} / ${usd(pf.data?.realized_week)}`}
          tone={Number(pf.data?.realized_today) < 0 ? "warn" : undefined} />
      </div>
      <div className="grid">
        <Card title="Open positions — HOLD / EXIT advisories" span={12}>
          {!open.length ? <Empty>No open positions. When you execute a recommended trade, record it from its case in the Research Laboratory so MERIDIAN can monitor it.</Empty> : (
            <table><thead><tr><th>Position</th><th>Entry</th><th>Stop</th><th>Target</th><th>Risk to stop</th><th>Last / P&L</th><th>Advice</th><th /></tr></thead>
              <tbody>{open.map((p) => <PositionRow key={p.id} p={p} onDone={() => { pos.reload(); pf.reload(); }} />)}</tbody></table>)}
        </Card>

        <Card title="Risk alerts" span={6}>
          {!alerts.data?.length ? <Empty>No alerts.</Empty> : (
            <table><tbody>{alerts.data.map((a, i) => (
              <tr key={i}><td style={{ width: 110 }}>{fmtTime(a.ts)}</td><td><span className={`dot ${a.severity === "high" ? "bad" : "warn"}`} />{a.title}</td>
                <td><div className="codes">{a.codes?.map((c: string) => <span className="code" key={c}>{c}</span>)}</div></td></tr>))}</tbody></table>)}
        </Card>

        <Card title="Recent risk decisions" span={6}>
          {!decisions.data?.length ? <Empty>No risk decisions yet.</Empty> : (
            <table><thead><tr><th>When</th><th>Outcome</th><th>Size</th><th>Reasons</th></tr></thead>
              <tbody>{decisions.data.map((d) => (
                <tr key={d.id} className="click" onClick={() => d.case_id && (location.href = `/lab/?id=${d.case_id}`)}>
                  <td>{fmtTime(d.created_at)}</td><td><Badge v={d.outcome} /></td><td className="num">{d.sizing?.suggested_quantity} <span className="notice">{usd(d.sizing?.max_loss_usd)}</span></td>
                  <td><div className="codes">{d.reason_codes.map((c: string) => <span className="code" key={c}>{c}</span>)}</div></td></tr>))}</tbody></table>)}
        </Card>

        <Card title={`Risk configuration · v${active?.version ?? "?"}`} span={7}>
          <div className="notice" style={{ marginBottom: 8 }}>Changes create a new immutable version, require your password again and are written to the audit log. Values are decimals (0.10 = 10%). <code>margin_per_contract</code> takes USD per contract by asset from your broker, e.g. {"{\"GOLD\": \"350\"}"}.</div>
          <textarea rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
          <form className="form-row" style={{ marginTop: 8 }} onSubmit={async (e) => {
            e.preventDefault(); setCfgMsg(null);
            try { const parsed = JSON.parse(draft); const r = await post<any>("/risk/config", { config: parsed, reason, password: pw }); setCfgMsg(`Saved as v${r.version}`); setPw(""); setReason(""); cfg.reload(); }
            catch (x: any) { setCfgMsg(x.message); }
          }}>
            <label style={{ flex: 1 }}>Reason<input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={5} /></label>
            <label>Password<input type="password" value={pw} onChange={(e) => setPw(e.target.value)} required autoComplete="current-password" /></label>
            <button className="primary">Authorize change</button>
          </form>
          {cfgMsg && <div className="notice" style={{ marginTop: 6 }}>{cfgMsg}</div>}
        </Card>
        <Card title="Configuration history" span={5}>
          <table><thead><tr><th>Ver</th><th>Status</th><th>By</th><th>Reason</th><th>When</th></tr></thead>
            <tbody>{(cfg.data?.versions ?? []).map((v: any) => (
              <tr key={v.version}><td className="num">v{v.version}</td><td><Badge v={v.status === "ACTIVE" ? "ACTIVE" : "CLOSED"} title={v.status} /></td><td>{v.created_by}</td><td>{v.reason}</td><td>{fmtTime(v.created_at)}</td></tr>))}</tbody></table>
          <h3>Closed positions</h3>
          {!closed.length ? <Empty>None yet.</Empty> : (
            <table><tbody>{closed.map((p) => (
              <tr key={p.id}><td><Badge v={p.direction} /> {p.instrument_id}</td><td className="num">{p.entry_price} → {p.exit_price}</td><td className="num">{usd(p.realized_pnl)}</td></tr>))}</tbody></table>)}
        </Card>
      </div>
    </Shell>
  );
}
