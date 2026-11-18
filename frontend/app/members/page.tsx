"use client";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Sparkline, Stat } from "@/components/ui";
import { api, fmtTime, num, post, usd, usePoll } from "@/lib/api";

const pct = (v: any) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(2)}%`);
const signed = (v: any) => (v === null || v === undefined ? "—" : `${Number(v) >= 0 ? "+" : "−"}${usd(Math.abs(Number(v)))}`);
const tone = (v: any) => (v === null || v === undefined || Number(v) === 0 ? undefined : Number(v) > 0 ? "ok" : "bad");
const COLORS = ["#7aa2ff", "#f0b35a", "#4cc38a", "#e5484d", "#b58cff", "#3fc1c9", "#ff8fa3", "#c2c66b"];

function Ownership({ rows, unassigned }: { rows: any[]; unassigned: any }) {
  const parts = [...rows.filter((r) => Number(r.ownership_pct) > 0).map((r, i) => ({ name: r.name, pct: Number(r.ownership_pct), c: COLORS[i % COLORS.length] })),
    ...(unassigned ? [{ name: "Unassigned", pct: Number(unassigned.ownership_pct), c: "#555" }] : [])];
  return (
    <div>
      <div style={{ display: "flex", height: 22, borderRadius: 6, overflow: "hidden", border: "1px solid var(--line)" }}>
        {parts.map((p) => <div key={p.name} title={`${p.name} ${p.pct.toFixed(2)}%`} style={{ width: `${p.pct}%`, background: p.c }} />)}
      </div>
      <div className="notice" style={{ marginTop: 6, display: "flex", gap: 14, flexWrap: "wrap" }}>
        {parts.map((p) => <span key={p.name}><span style={{ color: p.c }}>■</span> {p.name} {p.pct.toFixed(2)}%</span>)}
      </div>
    </div>
  );
}

function AddMember({ onDone }: { onDone: (id: string) => void }) {
  const [name, setName] = useState(""), [amount, setAmount] = useState(""), [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      const { id } = await post<{ id: string }>("/members", { name, note });
      if (Number(amount) > 0) await post(`/members/${id}/ledger`, { kind: "CONTRIBUTION", amount, note: note || "initial investment" });
      setName(""); setAmount(""); setNote(""); onDone(id);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  return (
    <Card title="Add a person" span={12}>
      <div className="form-row">
        <label>Name<br /><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name" /></label>
        <label>Investing now (optional)<br /><input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="$ amount" inputMode="decimal" /></label>
        <label style={{ flex: 1 }}>Note<br /><input style={{ width: "100%" }} value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. joined Dec 2026" /></label>
        <button className="primary" disabled={!name.trim() || busy} onClick={submit}>Add person</button>
      </div>
      <ErrorNote error={err} />
      <p className="notice">New money buys units at today's value per unit, so a new person shares only in gains and losses from the day they join. It also adds to the account's capital.</p>
    </Card>
  );
}

function MemberTab({ id, onChange }: { id: string; onChange: () => void }) {
  const { data: m, error, reload } = usePoll<any>(`/members/${id}`, 30000);
  const [kind, setKind] = useState("CONTRIBUTION"), [amount, setAmount] = useState(""), [note, setNote] = useState("");
  const [name, setName] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  if (!m) return <ErrorNote error={error} />;
  const record = async () => {
    setBusy(true); setErr(null);
    try { await post(`/members/${id}/ledger`, { kind, amount, note }); setAmount(""); setNote(""); reload(); onChange(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const rename = async () => {
    if (!name?.trim()) return;
    try { await api(`/members/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }); setName(null); reload(); onChange(); }
    catch (e: any) { setErr(e.message); }
  };
  const values = (m.series ?? []).map((p: any) => Number(p.value));
  return (
    <div className="grid">
      <Card title={m.name} span={12} right={<span>{m.role === "OWNER" && <Badge v="OWNER" />} joined {fmtTime(m.joined)}</span>}>
        <div className="stats">
          <Stat label="Current value" value={usd(m.value)} />
          <Stat label="Ownership" value={pct(m.ownership_pct)} sub={`${num(m.units, 4)} units × ${usd(m.nav_per_unit)}`} />
          <Stat label="Put in" value={usd(m.contributed)} sub={Number(m.withdrawn) > 0 ? `${usd(m.withdrawn)} taken out` : "nothing taken out"} />
          <Stat label="Gain / loss" value={signed(m.gain)} tone={tone(m.gain)} sub={m.gain_pct === null ? "" : `${Number(m.gain_pct) >= 0 ? "+" : ""}${Number(m.gain_pct).toFixed(2)}% on money put in`} />
          <Stat label="Share of closed trades" value={signed(m.realized_pnl_share)} tone={tone(m.realized_pnl_share)}
            sub={`${m.stats.trades} trades · ${m.stats.winning} won · ${m.stats.losing} lost`} />
          <Stat label="Worst drop from peak" value={pct(m.stats.max_drawdown_pct)} sub={`best ${signed(m.stats.best)} · worst ${signed(m.stats.worst)}`} />
        </div>
      </Card>
      <Card title="Value over time" span={8}>
        {values.length >= 2 ? <Sparkline points={values} height={170} /> : <Empty>History appears after the first full day.</Empty>}
        <p className="notice">End-of-day value from closed trades. Open trades count once they're closed.</p>
      </Card>
      <Card title="Money in / out" span={4}>
        <div className="form-row">
          <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="CONTRIBUTION">Add money</option><option value="WITHDRAWAL">Take money out</option></select>
          <input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="$ amount" inputMode="decimal" style={{ width: 110 }} />
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note" style={{ flex: 1 }} />
          <button className="primary" disabled={!(Number(amount) > 0) || busy} onClick={record}>Record</button>
        </div>
        <ErrorNote error={err} />
        <p className="notice">Priced at today's {usd(m.nav_per_unit)} per unit. Entries are permanent; fix a mistake with an opposite entry.</p>
        <div className="form-row" style={{ marginTop: 12 }}>
          <input value={name ?? m.name} onChange={(e) => setName(e.target.value)} />
          <button disabled={name === null || !name.trim() || name === m.name} onClick={rename}>Rename</button>
        </div>
      </Card>
      <Card title="History" span={5}>
        <table><thead><tr><th>When</th><th>What</th><th>Amount</th><th>Units</th><th>Per unit</th></tr></thead>
          <tbody>{m.ledger.map((e: any) => (<tr key={e.id} title={e.note}><td>{fmtTime(e.effective_at)}</td>
            <td>{e.kind === "CONTRIBUTION" ? (e.original ? "Original capital" : "Added money") : "Took money out"}</td>
            <td className="num">{usd(e.amount)}</td><td className="num">{num(e.units, 4)}</td><td className="num">{usd(e.nav_per_unit)}</td></tr>))}</tbody></table>
      </Card>
      <Card title="Their share of each closed trade" span={7}>
        {!m.trades.length ? <Empty>No closed trades since this person joined.</Empty> : (
          <table><thead><tr><th>Closed</th><th>Trade</th><th>Whole trade</th><th>They owned</th><th>Their share</th></tr></thead>
            <tbody>{m.trades.map((t: any, i: number) => (<tr key={i}><td>{fmtTime(t.closed_at)}</td><td><Badge v={t.direction} /> {t.asset}</td>
              <td className="num">{signed(t.trade_pnl)}</td><td className="num">{pct(t.ownership_pct)}</td>
              <td className="num" style={{ color: Number(t.share) >= 0 ? "var(--ok)" : "var(--bad)" }}>{signed(t.share)}</td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}

export default function Members() {
  const ov = usePoll<any>("/members", 30000);
  const [tab, setTab] = useState<string>("all");
  const rows: any[] = ov.data?.members ?? [];
  return (
    <Shell title="Members">
      <ErrorNote error={ov.error} />
      <div className="form-row" style={{ marginBottom: 12 }} role="tablist">
        <button className={tab === "all" ? "primary" : ""} onClick={() => setTab("all")}>All members</button>
        {rows.map((r) => <button key={r.id} className={tab === r.id ? "primary" : ""} onClick={() => setTab(r.id)}>{r.name}</button>)}
        <button className={tab === "add" ? "primary" : ""} onClick={() => setTab("add")}>+ Add person</button>
      </div>
      {tab === "all" && (
        <div className="grid">
          <Card title="Who owns the account" span={12}
            right={ov.data && <span className="notice">equity {usd(ov.data.equity)} · {num(ov.data.total_units, 2)} units · {usd(ov.data.nav_per_unit)} per unit</span>}>
            {!rows.length ? <Empty>No members yet. Add the first person.</Empty> : (<>
              <Ownership rows={rows} unassigned={ov.data?.unassigned} />
              <table style={{ marginTop: 12 }}><thead><tr><th>Name</th><th>Ownership</th><th>Value</th><th>Put in</th><th>Taken out</th><th>Gain / loss</th><th>Joined</th></tr></thead>
                <tbody>{rows.map((r) => (<tr key={r.id} onClick={() => setTab(r.id)} style={{ cursor: "pointer" }}>
                  <td><b>{r.name}</b> {r.role === "OWNER" && <Badge v="OWNER" />}</td><td className="num">{pct(r.ownership_pct)}</td>
                  <td className="num">{usd(r.value)}</td><td className="num">{usd(r.contributed)}</td><td className="num">{usd(r.withdrawn)}</td>
                  <td className="num" style={{ color: Number(r.gain) >= 0 ? "var(--ok)" : "var(--bad)" }}>{signed(r.gain)} {r.gain_pct !== null && `(${Number(r.gain_pct).toFixed(1)}%)`}</td>
                  <td>{fmtTime(r.joined)}</td></tr>))}
                  {ov.data?.unassigned && <tr><td><i>Unassigned capital</i></td><td className="num">{pct(ov.data.unassigned.ownership_pct)}</td><td className="num">{usd(ov.data.unassigned.value)}</td><td colSpan={4} className="notice">Capital not yet linked to a person</td></tr>}
                </tbody></table></>)}
            <p className="notice">{ov.data?.note} Each closed trade is split by ownership at the moment it closes.</p>
          </Card>
        </div>)}
      {tab === "add" && <div className="grid"><AddMember onDone={(id) => { ov.reload(); setTab(id); }} /></div>}
      {tab !== "all" && tab !== "add" && <MemberTab key={tab} id={tab} onChange={ov.reload} />}
    </Shell>
  );
}
