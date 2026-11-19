"use client";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Sparkline, Stat } from "@/components/ui";
import { api, fmtTime, post, usd, usePoll } from "@/lib/api";

const pct = (v: any, d = 2) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`);
const signed = (v: any) => (v === null || v === undefined ? "—" : `${Number(v) >= 0 ? "+" : "−"}${usd(Math.abs(Number(v)))}`);
const tone = (v: any) => (v === null || v === undefined || Number(v) === 0 ? undefined : Number(v) > 0 ? "ok" : "bad");
const COLORS = ["#7aa2ff", "#f0b35a", "#4cc38a", "#e5484d", "#b58cff", "#3fc1c9", "#ff8fa3", "#c2c66b"];
const CLASS: Record<string, string> = { A: "Class A · Founders (voting)", B: "Class B · Strategic partners (non-voting)", C: "Class C · Employees (vesting)" };
const PERIOD: Record<string, string> = { MONTHLY: "monthly", QUARTERLY: "quarterly", ANNUAL: "yearly" };

function Bar({ parts }: { parts: { name: string; pct: number; c: string }[] }) {
  const shown = parts.filter((p) => p.pct > 0);
  return (
    <div>
      <div style={{ display: "flex", height: 22, borderRadius: 6, overflow: "hidden", border: "1px solid var(--line)" }}>
        {shown.map((p) => <div key={p.name} title={`${p.name} ${p.pct.toFixed(2)}%`} style={{ width: `${p.pct}%`, background: p.c }} />)}
      </div>
      <div className="notice" style={{ marginTop: 6, display: "flex", gap: 14, flexWrap: "wrap" }}>
        {shown.map((p) => <span key={p.name}><span style={{ color: p.c }}>■</span> {p.name} {p.pct.toFixed(2)}%</span>)}
      </div>
    </div>
  );
}

function Pw({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return <label>Your password<br /><input type="password" value={value} onChange={(e) => onChange(e.target.value)} style={{ width: 150 }} /></label>;
}

function FundTab({ d, open }: { d: any; open: (id: string) => void }) {
  const rows: any[] = d.members;
  const parts = [...rows.map((r, i) => ({ name: r.name, pct: Number(r.ownership_pct), c: COLORS[i % COLORS.length] })),
    { name: "Management company (fees)", pct: Number(d.management_company.ownership_pct), c: "#8a8f98" },
    ...(d.unassigned ? [{ name: "Unassigned", pct: Number(d.unassigned.ownership_pct), c: "#444" }] : [])];
  const t = d.terms;
  return (
    <div className="grid">
      <Card title={`${d.fund_name} · investor capital`} span={12}
        right={<span className="notice">fund {usd(d.fund_total)} · {t ? `${Number(t.mgmt_fee_pct)}% management / ${Number(t.perf_alloc_pct)}% performance, ${PERIOD[t.perf_period]}` : "no fee terms"}</span>}>
        <Bar parts={parts} />
        <table style={{ marginTop: 12 }}><thead><tr><th>Investor</th><th>Share of fund</th><th>Capital account</th><th>Open P&L</th><th>Put in</th><th>Taken out</th><th>Gain / loss</th><th>Fees paid</th><th>Joined</th></tr></thead>
          <tbody>{rows.map((r) => (<tr key={r.id} onClick={() => open(r.id)} style={{ cursor: "pointer" }}>
            <td><b>{r.name}</b> {r.role === "OWNER" && <Badge v="OWNER" />} {r.fee_exempt && <span className="notice">fee-exempt</span>}</td>
            <td className="num">{pct(r.ownership_pct)}</td><td className="num">{usd(r.value)}</td>
            <td className="num" style={{ color: Number(r.open_pnl) > 0 ? "var(--ok)" : Number(r.open_pnl) < 0 ? "var(--bad)" : undefined }}>{signed(r.open_pnl)}</td>
            <td className="num">{usd(r.contributed)}</td>
            <td className="num">{usd(r.withdrawn)}</td>
            <td className="num" style={{ color: Number(r.gain) >= 0 ? "var(--ok)" : "var(--bad)" }}>{signed(r.gain)} {r.gain_pct !== null && `(${Number(r.gain_pct).toFixed(1)}%)`}</td>
            <td className="num">{usd(Number(r.fees_paid.management) + Number(r.fees_paid.performance))}</td><td>{fmtTime(r.joined)}</td></tr>))}
            <tr><td><i>MERIDIAN Capital Management</i> <span className="notice">fees earned, kept in the fund</span></td><td className="num">{pct(d.management_company.ownership_pct)}</td>
              <td className="num">{usd(d.management_company.value)}</td><td colSpan={6} /></tr>
            {d.unassigned && <tr><td><i>Unassigned capital</i></td><td className="num">{pct(d.unassigned.ownership_pct)}</td><td className="num">{usd(d.unassigned.value)}</td>
              <td colSpan={6} className="notice">Not linked to a person yet: add someone with "from the original capital" ticked</td></tr>}
          </tbody></table>
        <p className="notice">Values include open positions at the latest price. When money goes in or out, open positions are valued at that moment and their gain or loss so far stays with the people who held them, so people who join later share only in results after they join.</p>
      </Card>
    </div>
  );
}

function CompanyTab({ d, reload }: { d: any; reload: () => void }) {
  const co = d.company;
  const [who, setWho] = useState(""), [cls, setCls] = useState("B"), [p, setP] = useState(""), [vest, setVest] = useState("0"), [cliff, setCliff] = useState("0");
  const [note, setNote] = useState(""), [pw, setPw] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const issue = async () => {
    setBusy(true); setErr(null);
    try {
      await post("/members/company/grants", { member_id: who, equity_class: cls, pct: p, vesting_months: Number(vest), cliff_months: Number(cliff), note, password: pw });
      setP(""); setNote(""); setPw(""); reload();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const end = async (id: number) => {
    const password = window.prompt("Your password to end this grant (its equity returns to the founder):");
    if (!password) return;
    try { await post(`/members/company/grants/${id}/end`, { password }); reload(); } catch (e: any) { setErr(e.message); }
  };
  const isFounder = (g: any) => g.class === "A" && d.members.find((m: any) => m.id === g.member_id)?.role === "OWNER";
  return (
    <div className="grid">
      <Card title="MERIDIAN Capital Management · who owns the business" span={12}>
        <div className="stats">
          <Stat label="Fees earned" value={usd(co.fees_earned.total)} sub={`${usd(co.fees_earned.management)} management · ${usd(co.fees_earned.performance)} performance`} />
          <Stat label="Company's money in the fund" value={usd(co.capital_in_fund)} sub="fees plus their share of later trades" />
          <Stat label="Performance fee if the period ended now" value={usd(co.performance_fee_if_period_ended_now)} sub={d.terms ? `charged ${PERIOD[d.terms.perf_period]}` : ""} />
          <Stat label="Equity issued" value={pct(co.issued_pct)} sub={Number(co.unissued_pct) > 0 ? `${pct(co.unissued_pct)} unissued` : "fully issued"} />
        </div>
        <Bar parts={co.grants.map((g: any, i: number) => ({ name: `${g.name} (${g.class})`, pct: Number(g.pct), c: COLORS[i % COLORS.length] }))} />
        <table style={{ marginTop: 12 }}><thead><tr><th>Holder</th><th>Class</th><th>Owns</th><th>Vested</th><th>Votes</th><th>Share of company value</th><th>Since</th><th /></tr></thead>
          <tbody>{co.grants.map((g: any) => (<tr key={g.id} title={g.note}>
            <td><b>{g.name}</b></td><td>{CLASS[g.class]}</td><td className="num">{pct(g.pct)}</td>
            <td className="num">{pct(g.vested_pct)} {g.vesting_months > 0 && <span className="notice">over {g.vesting_months} mo{g.cliff_months ? `, ${g.cliff_months}-mo cliff` : ""}</span>}</td>
            <td>{g.voting ? "Yes" : "No"}</td><td className="num">{usd(g.share_of_company_value)}</td><td>{fmtTime(g.vesting_start)}</td>
            <td>{!isFounder(g) && <button className="link" onClick={() => end(g.id)}>End</button>}</td></tr>))}</tbody></table>
        <p className="notice">Putting money into the fund does not give anyone a piece of the company. Company equity is a separate grant that comes out of the founder's stake.</p>
      </Card>
      <Card title="Issue company equity" span={12}>
        <div className="form-row">
          <label>Person<br /><select value={who} onChange={(e) => setWho(e.target.value)}><option value="">Choose…</option>{d.members.map((m: any) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
          <label>Class<br /><select value={cls} onChange={(e) => setCls(e.target.value)}>{["A", "B", "C"].map((c) => <option key={c} value={c}>{CLASS[c]}</option>)}</select></label>
          <label>% of company<br /><input value={p} onChange={(e) => setP(e.target.value)} inputMode="decimal" style={{ width: 90 }} placeholder="e.g. 2" /></label>
          <label>Vests over (months)<br /><input value={vest} onChange={(e) => setVest(e.target.value)} inputMode="numeric" style={{ width: 90 }} /></label>
          <label>Cliff (months)<br /><input value={cliff} onChange={(e) => setCliff(e.target.value)} inputMode="numeric" style={{ width: 90 }} /></label>
          <label style={{ flex: 1 }}>Note<br /><input value={note} onChange={(e) => setNote(e.target.value)} style={{ width: "100%" }} placeholder="what it's for" /></label>
          <Pw value={pw} onChange={setPw} />
          <button className="primary" disabled={!who || !(Number(p) > 0) || !pw || busy} onClick={issue}>Issue</button>
        </div>
        <ErrorNote error={err} />
      </Card>
    </div>
  );
}

function TermsTab({ d, reload }: { d: any; reload: () => void }) {
  const t = d.terms ?? {};
  const [m, setM] = useState(String(Number(t.mgmt_fee_pct ?? 2))), [p, setP] = useState(String(Number(t.perf_alloc_pct ?? 20)));
  const [h, setH] = useState(String(Number(t.hurdle_pct ?? 0))), [hwm, setHwm] = useState<boolean>(t.high_water_mark ?? true), [per, setPer] = useState<string>(t.perf_period ?? "QUARTERLY");
  const [reason, setReason] = useState(""), [pw, setPw] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setErr(null);
    try { await post("/members/terms", { mgmt_fee_pct: m, perf_alloc_pct: p, hurdle_pct: h, high_water_mark: hwm, perf_period: per, reason, password: pw }); setReason(""); setPw(""); reload(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const ex = 1000, gain = 150;
  const mgmtEx = ex * Number(t.mgmt_fee_pct ?? 0) / 100, perfEx = Math.max(gain - ex * Number(t.hurdle_pct ?? 0) / 100, 0) * Number(t.perf_alloc_pct ?? 0) / 100;
  return (
    <div className="grid">
      <Card title="Current fund terms" span={6}>
        {d.terms ? (<>
          <div className="stats">
            <Stat label="Management fee" value={pct(t.mgmt_fee_pct)} sub="per year of capital, charged continuously" />
            <Stat label="Performance fee" value={pct(t.perf_alloc_pct)} sub={`of profit, ${PERIOD[t.perf_period]}${t.high_water_mark ? ", above high-water mark" : ""}`} />
            <Stat label="Hurdle" value={pct(t.hurdle_pct)} sub="yearly return investors keep first" />
          </div>
          <p className="notice">In effect since {fmtTime(t.effective_at)}. Founder capital and anyone marked fee-exempt pay nothing. Changes apply from the moment they're saved, never backwards.</p>
          <p className="notice">Example: an investor with {usd(ex)} who makes {usd(gain)} in a year pays about {usd(mgmtEx)} management fee plus {usd(perfEx)} performance fee. With a high-water mark nobody pays a performance fee twice on the same dollars: after a losing stretch, no performance fee until they're back above their previous peak.</p>
        </>) : <Empty>No terms set.</Empty>}
      </Card>
      <Card title="Change terms" span={6}>
        <div className="form-row">
          <label>Management %/yr<br /><input value={m} onChange={(e) => setM(e.target.value)} style={{ width: 90 }} inputMode="decimal" /></label>
          <label>Performance %<br /><input value={p} onChange={(e) => setP(e.target.value)} style={{ width: 90 }} inputMode="decimal" /></label>
          <label>Hurdle %/yr<br /><input value={h} onChange={(e) => setH(e.target.value)} style={{ width: 90 }} inputMode="decimal" /></label>
          <label>Charged<br /><select value={per} onChange={(e) => setPer(e.target.value)}><option value="MONTHLY">Monthly</option><option value="QUARTERLY">Quarterly</option><option value="ANNUAL">Yearly</option></select></label>
          <label><input type="checkbox" checked={hwm} onChange={(e) => setHwm(e.target.checked)} /> High-water mark</label>
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <label style={{ flex: 1 }}>Reason<br /><input value={reason} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} placeholder="e.g. lower fees while the pool is small" /></label>
          <Pw value={pw} onChange={setPw} />
          <button className="primary" disabled={reason.trim().length < 3 || !pw || busy} onClick={save}>Save terms</button>
        </div>
        <ErrorNote error={err} />
        <table style={{ marginTop: 12 }}><thead><tr><th>From</th><th>Mgmt</th><th>Perf</th><th>Hurdle</th><th>HWM</th><th>Why</th></tr></thead>
          <tbody>{(d.terms_history ?? []).map((x: any) => (<tr key={x.id}><td>{fmtTime(x.effective_at)}</td><td>{pct(x.mgmt_fee_pct)}</td><td>{pct(x.perf_alloc_pct)}</td>
            <td>{pct(x.hurdle_pct)}</td><td>{x.high_water_mark ? "Yes" : "No"}</td><td className="notice">{x.reason}</td></tr>))}</tbody></table>
      </Card>
    </div>
  );
}

function PersonTab({ id, onChange }: { id: string; onChange: () => void }) {
  const { data: m, error, reload } = usePoll<any>(`/members/${id}`, 30000);
  const [kind, setKind] = useState("CONTRIBUTION"), [amount, setAmount] = useState(""), [note, setNote] = useState("");
  const [original, setOriginal] = useState(false), [name, setName] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  if (!m) return <ErrorNote error={error} />;
  const record = async () => {
    setBusy(true); setErr(null);
    try { await post(`/members/${id}/ledger`, { kind, amount, note, original: kind === "CONTRIBUTION" && original }); setAmount(""); setNote(""); setOriginal(false); reload(); onChange(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const patch = async (body: any) => {
    try { await api(`/members/${id}`, { method: "PATCH", body: JSON.stringify(body) }); setName(null); reload(); onChange(); }
    catch (e: any) { setErr(e.message); }
  };
  const values = (m.series ?? []).map((p: any) => Number(p.value));
  const fees = Number(m.fees_paid.management) + Number(m.fees_paid.performance);
  const history = [...m.ledger.map((e: any) => ({ t: e.effective_at, w: e.kind === "CONTRIBUTION" ? (e.original ? "Original capital" : "Added money") : "Took money out", a: e.amount, n: e.note })),
    ...m.fee_history.map((f: any) => ({ t: f.at, w: "Performance fee", a: -Number(f.amount), n: "" }))].sort((a, b) => (a.t < b.t ? 1 : -1));
  return (
    <div className="grid">
      <Card title={m.name} span={12} right={<span>{m.role === "OWNER" && <Badge v="OWNER" />} joined {fmtTime(m.joined)}</span>}>
        <div className="stats">
          <Stat label="Capital account" value={usd(m.value)} sub={`${pct(m.ownership_pct)} of the fund · ${signed(m.open_pnl)} open`} />
          <Stat label="Put in" value={usd(m.contributed)} sub={Number(m.withdrawn) > 0 ? `${usd(m.withdrawn)} taken out` : "nothing taken out"} />
          <Stat label="Gain / loss after fees" value={signed(m.gain)} tone={tone(m.gain)} sub={m.gain_pct === null ? "" : `${Number(m.gain_pct) >= 0 ? "+" : ""}${Number(m.gain_pct).toFixed(2)}% on money put in`} />
          <Stat label="Share of closed trades" value={signed(m.realized_pnl_share)} tone={tone(m.realized_pnl_share)} sub={`${m.stats.trades} trades · ${m.stats.winning} won · ${m.stats.losing} lost`} />
          <Stat label="Fees paid" value={m.fee_exempt ? "Exempt" : usd(fees)}
            sub={m.fee_exempt ? "pays no fees" : `${usd(m.fees_paid.management)} mgmt · ${usd(m.fees_paid.performance)} perf · ${usd(m.performance_fee_if_period_ended_now)} accruing`} />
          <Stat label="Worst drop from peak" value={pct(m.stats.max_drawdown_pct)} sub={m.high_water_mark ? `high-water mark ${usd(m.high_water_mark)}` : ""} />
        </div>
      </Card>
      <Card title="Value over time" span={8}>
        {values.length >= 2 ? <Sparkline points={values} height={170} /> : <Empty>History appears after the first full day.</Empty>}
        <p className="notice">End-of-day capital account, after fees. Open trades count once they're closed.</p>
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
        {kind === "CONTRIBUTION" && <label className="notice"><input type="checkbox" checked={original} onChange={(e) => setOriginal(e.target.checked)} /> from the original capital (already in the account)</label>}
        <ErrorNote error={err} />
        <p className="notice">Entries are permanent; fix a mistake with an opposite entry.</p>
        <div className="form-row" style={{ marginTop: 12 }}>
          <input value={name ?? m.name} onChange={(e) => setName(e.target.value)} />
          <button disabled={name === null || !name.trim() || name === m.name} onClick={() => patch({ name })}>Rename</button>
        </div>
        <label className="notice" style={{ display: "block", marginTop: 8 }}>
          <input type="checkbox" checked={m.fee_exempt} onChange={(e) => patch({ fee_exempt: e.target.checked })} /> Fee-exempt (applies from now on)
        </label>
      </Card>
      <PortalLogin id={id} name={m.name} />
      <Card title="Management company equity" span={4}>
        {!m.company_equity.length ? <Empty>No equity in MERIDIAN Capital Management.</Empty> :
          m.company_equity.map((g: any) => <div key={g.id} className="note"><b>{pct(g.pct)}</b> {CLASS[g.class]} · {pct(g.vested_pct)} vested · {usd(g.share_of_company_value)}</div>)}
      </Card>
      <Card title="History" span={4}>
        <table><thead><tr><th>When</th><th>What</th><th>Amount</th></tr></thead>
          <tbody>{history.map((x, i) => <tr key={i} title={x.n}><td>{fmtTime(x.t)}</td><td>{x.w}</td><td className="num">{usd(x.a)}</td></tr>)}</tbody></table>
      </Card>
      <Card title="Share of each closed trade" span={4}>
        {!m.trades.length ? <Empty>No closed trades since this person joined.</Empty> : (
          <table><thead><tr><th>Closed</th><th>Trade</th><th>Owned</th><th>Their share</th></tr></thead>
            <tbody>{m.trades.map((t: any, i: number) => (<tr key={i} title={`whole trade ${signed(t.trade_pnl)}`}><td>{fmtTime(t.closed_at)}</td><td><Badge v={t.direction} /> {t.asset}</td>
              <td className="num">{pct(t.ownership_pct)}</td>
              <td className="num" style={{ color: Number(t.share) >= 0 ? "var(--ok)" : "var(--bad)" }}>{signed(t.share)}</td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}

function PortalLogin({ id, name }: { id: string; name: string }) {
  const { data: st, reload } = usePoll<any>(`/members/${id}/login`, 60000);
  const [user, setUser] = useState(""), [pw, setPw] = useState(""), [err, setErr] = useState<string | null>(null);
  const [issued, setIssued] = useState<any>(null);
  const create = async () => {
    setErr(null);
    try { setIssued(await post(`/members/${id}/login`, { username: user || st?.username, password: pw })); setPw(""); reload(); }
    catch (e: any) { setErr(e.message); }
  };
  const disable = async () => { try { await post(`/members/${id}/login/disable`, {}); reload(); } catch (e: any) { setErr(e.message); } };
  return (
    <Card title="Member portal login" span={4}>
      {st?.exists ? (
        <p>Username <b>{st.username}</b> · {st.disabled ? <Badge v="DISABLED" /> : st.must_change_password ? "waiting for first sign-in" : `last signed in ${st.last_login ? fmtTime(st.last_login) : "never"}`}</p>
      ) : <p className="notice">{name} has no login yet. Members see only their own account: value, gains, fees, history.</p>}
      <div className="form-row">
        <input placeholder="username" value={user} onChange={(e) => setUser(e.target.value)} style={{ width: 130 }} />
        <input type="password" placeholder="your password" value={pw} onChange={(e) => setPw(e.target.value)} style={{ width: 130 }} />
        <button className="primary" disabled={!pw || !(user || st?.username)} onClick={create}>{st?.exists ? "Reset" : "Create"}</button>
        {st?.exists && !st.disabled && <button className="danger" onClick={disable}>Disable</button>}
      </div>
      <ErrorNote error={err} />
      {issued && <div className="note" style={{ marginTop: 8 }}>Give {name} these once (they'll choose their own password at first sign-in):<br />
        Username <b>{issued.username}</b> · One-time password <b><code>{issued.temporary_password}</code></b><br />
        <span className="notice">Sign-in page: {typeof window !== "undefined" ? window.location.origin : ""}/login/. This password won't be shown again.</span></div>}
    </Card>
  );
}

function AddPerson({ onDone }: { onDone: (id: string) => void }) {
  const [name, setName] = useState(""), [amount, setAmount] = useState(""), [note, setNote] = useState("");
  const [original, setOriginal] = useState(false), [exempt, setExempt] = useState(false);
  const [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      const { id } = await post<{ id: string }>("/members", { name, note });
      if (exempt) await api(`/members/${id}`, { method: "PATCH", body: JSON.stringify({ fee_exempt: true }) });
      if (Number(amount) > 0) await post(`/members/${id}/ledger`, { kind: "CONTRIBUTION", amount, original, note: note || (original ? "share of the original capital" : "initial investment") });
      setName(""); setAmount(""); setNote(""); onDone(id);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  return (
    <div className="grid"><Card title="Add a person" span={12}>
      <div className="form-row">
        <label>Name<br /><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name" /></label>
        <label>Investing now (optional)<br /><input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="$ amount" inputMode="decimal" /></label>
        <label style={{ flex: 1 }}>Note<br /><input style={{ width: "100%" }} value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. joined Dec 2026" /></label>
        <button className="primary" disabled={!name.trim() || busy} onClick={submit}>Add person</button>
      </div>
      <div className="form-row" style={{ marginTop: 8 }}>
        <label className="notice"><input type="checkbox" checked={original} onChange={(e) => setOriginal(e.target.checked)} /> from the original capital (already in the account)</label>
        <label className="notice"><input type="checkbox" checked={exempt} onChange={(e) => setExempt(e.target.checked)} /> fee-exempt</label>
      </div>
      <ErrorNote error={err} />
      <p className="notice">New money opens a capital account and shares only in results from the day it's added. It also adds to the account's capital. A person can hold fund capital, company equity, or both.</p>
    </Card></div>
  );
}

export default function Members() {
  const ov = usePoll<any>("/members", 30000);
  const [tab, setTab] = useState<string>("fund");
  const d = ov.data;
  const rows: any[] = d?.members ?? [];
  const T = (id: string, label: string) => <button key={id} className={tab === id ? "primary" : ""} onClick={() => setTab(id)}>{label}</button>;
  return (
    <Shell title="Members">
      <ErrorNote error={ov.error} />
      <div className="form-row" style={{ marginBottom: 8 }} role="tablist">
        {T("fund", "Fund I")}{T("company", "Management company")}{T("terms", "Fund terms")}
        <span className="notice" style={{ margin: "0 4px" }}>|</span>
        {rows.map((r) => T(r.id, r.name))}{T("add", "+ Add person")}
      </div>
      {!d ? null : tab === "fund" ? <FundTab d={d} open={setTab} />
        : tab === "company" ? <CompanyTab d={d} reload={ov.reload} />
        : tab === "terms" ? <TermsTab key={d.terms?.id} d={d} reload={ov.reload} />
        : tab === "add" ? <AddPerson onDone={(id) => { ov.reload(); setTab(id); }} />
        : <PersonTab key={tab} id={tab} onChange={ov.reload} />}
      <p className="notice" style={{ marginTop: 16 }}>Record-keeping only. Before taking outside money, have a securities/commodities attorney set up the actual entities and offering documents; performance fees in particular are limited to qualified investors.</p>
    </Shell>
  );
}
