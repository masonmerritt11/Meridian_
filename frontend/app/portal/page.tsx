"use client";
import { useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorNote, Sparkline, Stat } from "@/components/ui";
import { api, fmtTime, post, usd, usePoll } from "@/lib/api";

const pct = (v: any, d = 2) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`);
const signed = (v: any) => (v === null || v === undefined ? "—" : `${Number(v) >= 0 ? "+" : "−"}${usd(Math.abs(Number(v)))}`);
const tone = (v: any) => (v === null || v === undefined || Number(v) === 0 ? undefined : Number(v) > 0 ? "ok" : "bad");
const PERIOD: Record<string, string> = { MONTHLY: "monthly", QUARTERLY: "quarterly", ANNUAL: "yearly" };

function PasswordForm({ forced, onDone }: { forced: boolean; onDone: () => void }) {
  const [cur, setCur] = useState(""), [n1, setN1] = useState(""), [n2, setN2] = useState("");
  const [err, setErr] = useState<string | null>(null), [ok, setOk] = useState(false);
  const save = async () => {
    setErr(null);
    if (n1 !== n2) return setErr("The new passwords don't match.");
    try { await post("/auth/password", { current: cur, new: n1 }); setOk(true); setCur(""); setN1(""); setN2(""); onDone(); }
    catch (e: any) { setErr(e.message); }
  };
  return (
    <Card title={forced ? "Choose your own password to continue" : "Change password"} span={forced ? 12 : 4}>
      {forced && <p>You signed in with a one-time password. Please set your own (at least 10 characters).</p>}
      <div className="form-row">
        <label>{forced ? "One-time password" : "Current password"}<br /><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" /></label>
        <label>New password<br /><input type="password" value={n1} onChange={(e) => setN1(e.target.value)} autoComplete="new-password" /></label>
        <label>Repeat<br /><input type="password" value={n2} onChange={(e) => setN2(e.target.value)} autoComplete="new-password" /></label>
        <button className="primary" disabled={!cur || n1.length < 10} onClick={save}>Save</button>
      </div>
      <ErrorNote error={err} />
      {ok && <p className="notice">Password updated.</p>}
    </Card>
  );
}

export default function Portal() {
  const [me, setMe] = useState<any>(null);
  const loadMe = () => api("/auth/me").then((m: any) => { if (m.role !== "MEMBER") location.href = "/"; else setMe(m); }).catch(() => (location.href = "/login/"));
  useEffect(() => { loadMe(); }, []);
  const ready = me && !me.must_change_password;
  const { data: a, error } = usePoll<any>(ready ? "/me/account" : null, 60000);
  const values = (a?.series ?? []).map((p: any) => Number(p.value));
  const fees = a ? Number(a.fees_paid.management) + Number(a.fees_paid.performance) : 0;
  const history = a ? [...a.ledger.map((e: any) => ({ t: e.effective_at, w: e.kind === "CONTRIBUTION" ? (e.original ? "Original investment" : "Added money") : "Withdrawal", a: e.amount })),
    ...a.fee_history.map((f: any) => ({ t: f.at, w: "Performance fee", a: -Number(f.amount) }))].sort((x, y) => (x.t < y.t ? 1 : -1)) : [];
  return (
    <div style={{ maxWidth: 1180, margin: "0 auto", padding: "24px 20px" }}>
      <div className="form-row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div className="brand" style={{ padding: 0 }}>
          <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.6" /><line x1="12" y1="1.5" x2="12" y2="22.5" stroke="currentColor" strokeWidth="1.6" /></svg>
          <span>MERIDIAN</span> <span className="notice" style={{ marginLeft: 8 }}>Member portal</span>
        </div>
        <span className="notice">{me?.username} · <button className="link" onClick={() => post("/auth/logout", {}).then(() => (location.href = "/login/"))}>Sign out</button></span>
      </div>
      {me?.must_change_password ? <div className="grid"><PasswordForm forced onDone={loadMe} /></div> : !a ? <ErrorNote error={error} /> : (
        <div className="grid">
          <Card title={`${a.name} · ${a.fund_name}`} span={12} right={<span className="notice">investor since {fmtTime(a.joined)}</span>}>
            <div className="stats">
              <Stat label="Your account" value={usd(a.value)} sub={`${pct(a.ownership_pct)} of the fund`} />
              <Stat label="You put in" value={usd(a.contributed)} sub={Number(a.withdrawn) > 0 ? `${usd(a.withdrawn)} withdrawn` : "nothing withdrawn"} />
              <Stat label="Gain / loss" value={signed(a.gain)} tone={tone(a.gain)} sub={a.gain_pct === null ? "" : `${Number(a.gain_pct) >= 0 ? "+" : ""}${Number(a.gain_pct).toFixed(2)}% after fees`} />
              <Stat label="Closed trades" value={a.stats.trades} sub={`${a.stats.winning} won · ${a.stats.losing} lost`} />
              <Stat label="Fees" value={a.fee_exempt ? "None" : usd(fees)} sub={a.fee_exempt ? "fee-exempt" : `${usd(a.performance_fee_if_period_ended_now)} accruing this period`} />
              <Stat label="Worst drop from peak" value={pct(a.stats.max_drawdown_pct)} />
            </div>
          </Card>
          <Card title="Your account over time" span={8}>
            {values.length >= 2 ? <Sparkline points={values} height={190} /> : <Empty>Your chart fills in after the first full day.</Empty>}
            <p className="notice">End-of-day value after fees. Open trades are counted once they close.</p>
          </Card>
          <Card title="How the fund works" span={4}>
            <ul className="list">
              <li>Your money is pooled with the other investors and traded by MERIDIAN.</li>
              <li>Each closed trade's profit or loss is shared by account size at the moment it closes. You share only in results from the day you joined.</li>
              {a.terms && !a.fee_exempt && <li>Fees: {Number(a.terms.mgmt_fee_pct)}% a year management, plus {Number(a.terms.perf_alloc_pct)}% of profit {PERIOD[a.terms.perf_period]}{a.terms.high_water_mark ? ", only on new highs (high-water mark)" : ""}.</li>}
              {a.high_water_mark && <li>Your high-water mark: {usd(a.high_water_mark)}.</li>}
            </ul>
          </Card>
          <Card title="Your share of each closed trade" span={8}>
            {!a.trades.length ? <Empty>No closed trades since you joined.</Empty> : (
              <table><thead><tr><th>Closed</th><th>Trade</th><th>Your share of the fund</th><th>Your result</th></tr></thead>
                <tbody>{a.trades.map((t: any, i: number) => (<tr key={i}><td>{fmtTime(t.closed_at)}</td><td><Badge v={t.direction} /> {t.asset}</td>
                  <td className="num">{pct(t.ownership_pct)}</td>
                  <td className="num" style={{ color: Number(t.share) >= 0 ? "var(--ok)" : "var(--bad)" }}>{signed(t.share)}</td></tr>))}</tbody></table>)}
          </Card>
          <Card title="Deposits, withdrawals and fees" span={4}>
            <table><tbody>{history.map((x, i) => <tr key={i}><td>{fmtTime(x.t)}</td><td>{x.w}</td><td className="num">{usd(x.a)}</td></tr>)}</tbody></table>
            {a.company_equity.length > 0 && <><h4>MERIDIAN Capital Management</h4>{a.company_equity.map((g: any, i: number) => <p key={i} className="notice">You own {pct(g.pct)} (Class {g.class}{g.voting ? ", voting" : ""}) · {pct(g.vested_pct)} vested</p>)}</>}
          </Card>
          <PasswordForm forced={false} onDone={() => {}} />
          <div style={{ gridColumn: "span 8" }}><p className="notice">Figures are MERIDIAN's internal records of the fund. Past results don't guarantee future results. Questions about your account: contact Mason.</p></div>
        </div>)}
    </div>
  );
}
