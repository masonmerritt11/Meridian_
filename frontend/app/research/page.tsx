"use client";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, Stat } from "@/components/ui";
import { ago, fmtTime, post, usd, usePoll } from "@/lib/api";

const WHO: Record<string, string> = {
  "research.psychology": "Psychology",
  "research.philosophy": "Philosophy",
  "research.director": "Director",
};

function Note({ n }: { n: any }) {
  const [open, setOpen] = useState(n.kind === "SYNTHESIS");
  return (
    <div className={`note ${n.kind === "SYNTHESIS" ? "synthesis" : ""}`}>
      <div className="note-head" onClick={() => setOpen(!open)}>
        <span className="code">{WHO[n.worker_id] ?? n.worker_id}</span>
        <b>{n.title}</b>
        <span className="notice">{ago(n.created_at)}{n.sources?.length ? ` · ${n.sources.length} source${n.sources.length > 1 ? "s" : ""}` : ""}</span>
      </div>
      {n.kind !== "SYNTHESIS" && <div className="notice" style={{ margin: "2px 0 4px" }}>Topic: {n.topic}</div>}
      <p style={{ margin: "6px 0" }}>{n.summary}</p>
      {open && (<>
        {!!n.key_ideas?.length && (<><h4>Key ideas</h4><ul className="list">{n.key_ideas.map((k: string, i: number) => <li key={i}>{k}</li>)}</ul></>)}
        {n.contrarian_angle && <><h4>What the crowd gets wrong</h4><p>{n.contrarian_angle}</p></>}
        {n.applies_to_meridian && <><h4>How it applies to MERIDIAN</h4><p>{n.applies_to_meridian}</p></>}
        {n.evidence_quality && <><h4>Strength of evidence</h4><p>{n.evidence_quality}</p></>}
        {n.kind !== "SYNTHESIS" && !!n.sources?.length && (<><h4>Sources read</h4>
          <ul className="list">{n.sources.map((s: any, i: number) => (
            <li key={i}><a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a>
              {s.author && ` · ${s.author}`}{s.year && ` (${s.year})`} {s.opened ? <Badge v="OPENED" title="full text read" /> : <Badge v="SEARCH RESULT" title="seen in search results" />}</li>))}</ul></>)}
      </>)}
      <button className="link" onClick={() => setOpen(!open)}>{open ? "Show less" : "Read the full note"}</button>
    </div>
  );
}

export default function Research() {
  const [who, setWho] = useState<string>("");
  const j = usePoll<any>(`/research/journal${who ? `?worker=${who}` : ""}`, 30000);
  const [topic, setTopic] = useState("");
  const [tw, setTw] = useState("research.psychology");
  const [msg, setMsg] = useState<string | null>(null);
  const d = j.data;
  const synth = d?.notes?.find((n: any) => n.kind === "SYNTHESIS");
  const notes = (d?.notes ?? []).filter((n: any) => n.kind === "NOTE");
  const open = (d?.recommendations ?? []).filter((r: any) => r.status === "PROPOSED");
  const decided = (d?.recommendations ?? []).filter((r: any) => r.status !== "PROPOSED");
  const queue = (d?.queue ?? []).filter((q: any) => q.status === "QUEUED");

  return (
    <Shell title="Learning Journal">
      <ErrorNote error={j.error} />
      {d && !d.spend.enabled && <div className="error">Research department is unavailable: no ANTHROPIC_API_KEY is set on the server.</div>}
      {d?.spend?.last_error && <div className="notice" style={{ marginBottom: 10 }}>Last AI error ({ago(d.spend.last_error.created_at)}): {d.spend.last_error.error}</div>}
      <div className="stats">
        <Stat label="Notes written" value={d?.stats?.notes ?? "—"} sub="each one = a topic studied from real sources" />
        <Stat label="Recommendations waiting" value={d?.stats?.proposed ?? "—"} tone={d?.stats?.proposed ? "ok" : undefined} />
        <Stat label="Topics queued" value={queue.length} sub="what they read next" />
        <Stat label="AI spend today" value={`${usd(d?.spend?.today_usd)} / ${usd(d?.spend?.daily_cap_usd)}`} sub={`7 days ${usd(d?.spend?.last_7d_usd)} · ${d?.spend?.model ?? ""}`} />
      </div>
      <div className="grid">
        {synth && (
          <Card title="Today's synthesis from the Research Director" span={12} right={<span className="notice">{fmtTime(synth.created_at)}</span>}>
            <Note n={synth} />
          </Card>)}

        <Card title="Recommendations for you" span={12} right={<span className="notice">Accepting records your decision. Nothing changes in the live system automatically.</span>}>
          {!open.length ? <Empty>No open recommendations. They appear when a researcher finds an idea that fits MERIDIAN.</Empty> : (
            <div style={{ display: "grid", gap: 10 }}>{open.map((r: any) => (
              <div key={r.id} className="note">
                <div className="note-head"><span className="code">{WHO[r.worker_id]}</span><b>{r.title}</b>
                  <Badge v={r.confidence === "HIGH" ? "ALLOW" : r.confidence === "MEDIUM" ? "RESTRICT" : "NEUTRAL"} title={`${r.confidence} confidence`} />
                  <span className="notice">{r.component.replace("_", " ")} · {ago(r.created_at)}</span></div>
                <p><b>Proposal:</b> {r.proposal}</p>
                <p><b>Why:</b> {r.rationale}</p>
                {r.how_to_test && <p><b>How to test it first:</b> {r.how_to_test}</p>}
                <div className="form-row">
                  {(["ACCEPTED", "PARKED", "REJECTED"] as const).map((s) => (
                    <button key={s} className={s === "ACCEPTED" ? "primary" : s === "REJECTED" ? "danger" : ""}
                      onClick={async () => { try { await post(`/research/recommendations/${r.id}/decision`, { status: s, note: "" }); j.reload(); } catch (e: any) { setMsg(e.message); } }}>
                      {s === "ACCEPTED" ? "Accept" : s === "PARKED" ? "Park" : "Reject"}</button>))}
                </div>
              </div>))}</div>)}
          {!!decided.length && (<details style={{ marginTop: 10 }}><summary className="notice">{decided.length} decided</summary>
            <table><tbody>{decided.map((r: any) => (<tr key={r.id}><td><Badge v={r.status === "ACCEPTED" ? "ACCEPT" : r.status === "REJECTED" ? "REJECT" : "DEFER"} title={r.status} /></td>
              <td>{r.title}</td><td className="notice">{r.decided_by} · {fmtTime(r.decided_at)}</td></tr>))}</tbody></table></details>)}
        </Card>

        <Card title="What they've learned" span={8} right={
          <select value={who} onChange={(e) => setWho(e.target.value)}>
            <option value="">All researchers</option><option value="research.psychology">Psychology</option><option value="research.philosophy">Philosophy</option>
          </select>}>
          {!notes.length ? <Empty>No notes yet. Each researcher studies one topic about every 3 hours.</Empty> :
            <div style={{ display: "grid", gap: 10 }}>{notes.map((n: any) => <Note key={n.id} n={n} />)}</div>}
        </Card>

        <Card title="Reading list" span={4}>
          <form className="form-row" style={{ marginBottom: 10 }} onSubmit={async (e) => {
            e.preventDefault();
            try { await post("/research/topics", { worker_id: tw, topic }); setTopic(""); setMsg("Added to the front of the queue."); j.reload(); }
            catch (x: any) { setMsg(x.message); }
          }}>
            <label style={{ flex: 1 }}>Ask them to study<input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="e.g. Jesse Livermore's rules" minLength={5} required /></label>
            <select value={tw} onChange={(e) => setTw(e.target.value)}><option value="research.psychology">Psychology</option><option value="research.philosophy">Philosophy</option></select>
            <button className="primary">Add</button>
          </form>
          {msg && <div className="notice">{msg}</div>}
          {!queue.length ? <Empty>Queue empty.</Empty> : (
            <>{[queue.slice(0, 12), queue.slice(12)].map((part, k) => !part.length ? null : (
              <details key={k} open={k === 0}>{k === 1 && <summary className="notice">{part.length} more queued</summary>}
                <table><tbody>{part.map((q: any) => (
                  <tr key={q.id}><td><span className="code">{WHO[q.worker_id]}</span></td><td>{q.topic}</td>
                    <td>{q.origin === "CIO" ? <Badge v="ACCEPT" title="requested by you" /> : q.origin === "SUGGESTED" ? <span className="notice">follow-up</span> : null}</td></tr>))}</tbody></table>
              </details>))}</>)}
        </Card>
      </div>
    </Shell>
  );
}
