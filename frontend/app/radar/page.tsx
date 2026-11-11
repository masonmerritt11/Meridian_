"use client";
import Link from "next/link";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote } from "@/components/ui";
import { ago, fmtTime, num, usePoll } from "@/lib/api";

const PIPELINE = ["DETECTED", "RESEARCHING", "CHALLENGED", "RISK_REVIEW", "COMMITTEE_REVIEW", "RECOMMENDED", "DEFERRED", "BLOCKED"];

export default function Radar() {
  const inst = usePoll<any[]>("/marketdata/instruments", 30000);
  const fresh = usePoll<any[]>("/marketdata/freshness", 15000);
  const cases = usePoll<any[]>("/cases?limit=200", 10000);
  const macro = usePoll<any>("/macro/events", 60000);
  const fr = Object.fromEntries((fresh.data ?? []).map((f) => [f.dataset, f]));
  const open = (cases.data ?? []).filter((c) => PIPELINE.includes(c.state));

  return (
    <Shell title="Opportunity Radar">
      <ErrorNote error={inst.error || cases.error} />
      <div className="grid">
        <Card title="Universe — Gold · Silver · Oil · Bitcoin · Ethereum · XRP" span={12}>
          {!inst.data?.length ? <Empty>No instruments discovered yet.</Empty> : (
            <table><thead><tr><th>Asset</th><th>Instrument</th><th>Type</th><th>Status</th><th>Contract size</th><th>Tick</th><th>Expiry</th><th>Candles</th><th>Book</th><th>Source</th></tr></thead>
              <tbody>{inst.data.map((i) => {
                const c = Object.values(fr).find((f: any) => f.dataset.startsWith(`candles:${i.id}:`)) as any;
                const b = fr[`book:${i.id}`];
                return (
                  <tr key={i.id}>
                    <td><b>{i.asset}</b></td><td className="mono" title={i.display_name}>{i.id}</td><td>{i.product_type}</td>
                    <td><Badge v={i.status} title={i.status_detail} />{i.status !== "ACTIVE" && <div className="notice">{i.status_detail}</div>}</td>
                    <td className="num">{num(i.contract_size, 8)}</td><td className="num">{num(i.price_increment, 8)}</td>
                    <td>{i.contract_expiry ? fmtTime(i.contract_expiry) : "—"}</td>
                    <td><Badge v={c?.status ?? "UNAVAILABLE"} title={c?.detail} /> <span className="notice">{ago(c?.last_data_ts)}</span></td>
                    <td><Badge v={b?.status ?? "UNAVAILABLE"} title={b?.detail} /> <span className="notice">{b?.detail?.replace("spread_bps=", "") ?? ""}{b?.status === "FRESH" ? " bps" : ""}</span></td>
                    <td><Badge v={i.source === "SIMULATED" ? "SIMULATED" : i.source} /></td>
                  </tr>);
              })}</tbody></table>)}
        </Card>

        <Card title="Research pipeline" span={7}>
          <div className="codes" style={{ marginBottom: 10 }}>
            {PIPELINE.map((s) => <span className="code" key={s}>{s} {open.filter((c) => c.state === s).length}</span>)}
          </div>
          {!open.length ? <Empty>No open research cases. The trend analyst opens one when a breakout aligns with trend.</Empty> : (
            <table><thead><tr><th>Opened</th><th>Case</th><th>State</th><th>Strength</th><th>Risk</th><th>Committee</th></tr></thead>
              <tbody>{open.map((c) => (
                <tr key={c.id} className="click" onClick={() => (location.href = `/lab/?id=${c.id}`)}>
                  <td>{fmtTime(c.created_at)}</td><td><Badge v={c.direction} /> {c.asset} <span className="notice mono">{c.instrument_id}</span></td>
                  <td><Badge v={c.state} /></td><td className="num">{c.strength}</td><td><Badge v={c.risk_outcome} /></td><td><Badge v={c.recommendation} /></td>
                </tr>))}</tbody></table>)}
        </Card>

        <Card title="Macro calendar" span={5} right={<span className="notice">{macro.data?.status?.source} · <Badge v={macro.data?.status?.status} /></span>}>
          {macro.data?.status?.status !== "FRESH" && <div className="notice" style={{ marginBottom: 8 }}>Calendar {macro.data?.status?.status ?? "UNAVAILABLE"}: {macro.data?.status?.detail ?? "never fetched"}. Risk restricts new ideas while event data is missing.</div>}
          {!macro.data?.events?.length ? <Empty>No events stored.</Empty> : (
            <table><thead><tr><th>Time (local)</th><th>Event</th><th>Impact</th><th>Fcst / Prev</th></tr></thead>
              <tbody>{macro.data.events.filter((e: any) => e.impact === "High" || e.country === "USD").slice(0, 40).map((e: any) => (
                <tr key={e.id}><td>{e.event_time ? fmtTime(e.event_time) : "time unknown"}</td><td>{e.country} · {e.title}</td>
                  <td><Badge v={e.impact === "High" ? "BLOCK" : e.impact === "Medium" ? "RESTRICT" : "NEUTRAL"} title={e.impact} /></td>
                  <td className="num">{e.forecast ?? "—"} / {e.previous ?? "—"}</td></tr>))}</tbody></table>)}
        </Card>
        <div className="notice" style={{ gridColumn: "span 12" }}>Closed {"→"} <Link href="/archive/">Decision Archive</Link></div>
      </div>
    </Shell>
  );
}
