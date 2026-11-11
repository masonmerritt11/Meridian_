"use client";
import Link from "next/link";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote, human } from "@/components/ui";
import { ago, fmtTime, post, usd, usePoll } from "@/lib/api";

const DOT: Record<string, string> = { HEALTHY: "ok", DEGRADED: "warn", UNKNOWN: "warn", STALE: "bad", FAILING: "bad" };

function ScoreBar({ v }: { v: number | null }) {
  if (v === null || v === undefined) return <span className="notice">—</span>;
  const n = Number(v);
  return (
    <span className="mono" style={{ color: n > 0.15 ? "var(--ok)" : n < -0.15 ? "var(--bad)" : "var(--muted)" }}>
      {n > 0 ? "+" : ""}{n.toFixed(2)}
    </span>
  );
}

function WorkerDetail({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const { data, error, reload } = usePoll<any>(`/workers/${id}/detail`, 10000);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <Card title={id} span={12}><ErrorNote error={error} /></Card>;
  if (!data) return <Card title={id} span={12}><Empty>Loading…</Empty></Card>;
  const w = data.worker, st = data.stats;
  return (
    <Card title={`${w.name} · ${w.id}`} span={12}
      right={<span><Badge v={w.health} /> <Badge v={w.status} /> <button className="link" onClick={onClose}>Close</button></span>}>
      <div className="doing"><b>What it's doing:</b> {data.doing}</div>

      <div className="stats" style={{ marginTop: 12 }}>
        <div className="stat"><div className="stat-label">Runs (24h)</div><div className="stat-value">{st.runs_24h}</div>
          <div className="stat-sub">{st.succeeded_24h} ok · {st.failed_24h} failed</div></div>
        <div className={`stat ${st.success_rate_24h < 0.9 && st.runs_24h ? "bad" : ""}`}><div className="stat-label">Success rate</div>
          <div className="stat-value">{st.runs_24h ? `${Math.round(st.success_rate_24h * 100)}%` : "—"}</div></div>
        <div className="stat"><div className="stat-label">Avg duration</div><div className="stat-value">{st.avg_duration_ms ? `${Math.round(st.avg_duration_ms)} ms` : "—"}</div></div>
        <div className="stat"><div className="stat-label">AI spend (24h)</div><div className="stat-value">{usd(st.llm_spend_24h_usd)}</div></div>
        <div className="stat"><div className="stat-label">Last seen</div><div className="stat-value" style={{ fontSize: 16 }}>{ago(w.last_seen)}</div>
          <div className="stat-sub">{w.schedule_seconds ? `every ${Math.round(w.schedule_seconds / 60)} min` : "event-driven"}</div></div>
      </div>

      <div className="split">
        <div>
          <h3>Latest output</h3>
          {!data.latest_outputs.length ? <Empty>This worker reports through events and runs rather than assessments; see below.</Empty> : (
            <table><thead><tr><th>Subject</th><th>View</th><th>Bias</th><th>Detail</th></tr></thead>
              <tbody>{data.latest_outputs.map((o: any) => (
                <tr key={o.subject}><td><b>{o.subject}</b><div className="notice">{ago(o.created_at)}</div></td>
                  <td><Badge v={o.label} />{o.data?.stance && <div style={{ marginTop: 3 }}><Badge v={o.data.stance} /></div>}</td>
                  <td><ScoreBar v={o.score} /></td><td>{o.summary}</td></tr>))}</tbody></table>)}
          {data.research && (<>
            <h3>Reading next</h3>
            {data.research.reading_next.length ? <ol className="list">{data.research.reading_next.map((t: any, i: number) => <li key={i}>{t.topic} <span className="notice">({t.origin.toLowerCase()})</span></li>)}</ol> : <Empty>Queue empty.</Empty>}
            <h3>Recent notes</h3>
            {data.research.recent_notes.length ? <ul className="list">{data.research.recent_notes.map((n: any) => <li key={n.id}><Link href="/research/">{n.title}</Link> <span className="notice">{ago(n.created_at)}</span></li>)}</ul> : <Empty>No notes yet.</Empty>}
          </>)}
        </div>
        <div>
          <h3>Role</h3>
          <dl className="kv">
            <dt>Department</dt><dd>{w.department_id}</dd>
            <dt>Responsibility</dt><dd style={{ fontFamily: "inherit" }}>{w.responsibility}</dd>
            <dt>Version</dt><dd>{w.worker_version} · config v{w.config_version}</dd>
            <dt>Listens for</dt><dd>{w.subscribes?.join(", ") || "—"}</dd>
            <dt>Escalates to</dt><dd>{w.escalation_to}</dd>
            <dt>Events handled</dt><dd>{Object.entries(data.events_consumed).map(([k, v]) => `${human(k).toLowerCase()} ${v}`).join(" · ") || "—"}</dd>
          </dl>
          <div className="form-row" style={{ margin: "10px 0" }}>
            {w.implemented && <button onClick={async () => {
              try { const r = await post(`/workers/${w.id}/status`, { status: w.status === "ACTIVE" ? "INACTIVE" : "ACTIVE" }); setMsg(`now ${r.status}`); reload(); onChanged(); }
              catch (e: any) { setMsg(e.message); }
            }}>{w.status === "ACTIVE" ? "Pause worker" : "Activate worker"}</button>}
            <span className="notice">{msg}</span>
          </div>
          <h3>Events it produced</h3>
          {!data.events_produced.length ? <Empty>None recently.</Empty> : (
            <table><tbody>{data.events_produced.map((e: any, i: number) => (
              <tr key={i}><td>{fmtTime(e.occurred_at)}</td><td className="mono">{e.event_type}</td></tr>))}</tbody></table>)}
          <details style={{ marginTop: 8 }}><summary className="notice">Configuration and input/output schema</summary>
            <pre>{JSON.stringify({ config: w.config, input: w.input_schema, output: w.output_schema }, null, 1)}</pre></details>
        </div>
      </div>

      <h3>Run history</h3>
      {!data.runs.length ? <Empty>No executions recorded.</Empty> : (
        <table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Tries</th><th>Duration</th><th>Result</th><th>Inputs used</th></tr></thead>
          <tbody>{data.runs.map((r: any) => (
            <tr key={r.id}><td>{fmtTime(r.started_at)}</td><td>{r.trigger.toLowerCase()}</td><td><Badge v={r.status} /></td>
              <td className="num">{r.attempts}</td><td className="num">{r.metrics?.duration_ms ? `${Math.round(r.metrics.duration_ms)} ms` : "—"}</td>
              <td>{r.output?.summary}{r.error && <pre style={{ color: "var(--bad)" }}>{r.error.split("\n").filter(Boolean).slice(-2).join("\n")}</pre>}</td>
              <td className="notice">{(r.input_refs ?? []).map((x: any) => x.kind).join(", ") || "—"}</td></tr>))}</tbody></table>)}
    </Card>
  );
}

export default function Operations() {
  const depts = usePoll<any[]>("/departments", 15000);
  const events = usePoll<any[]>("/events?limit=60", 10000);
  const dlq = usePoll<any[]>("/events/dlq", 30000);
  const [sel, setSel] = useState<string | null>(null);
  const total = (depts.data ?? []).reduce((n, d) => n + d.roles, 0);
  const impl = (depts.data ?? []).reduce((n, d) => n + d.implemented, 0);

  return (
    <Shell title="Department Operations">
      <ErrorNote error={depts.error} />
      <div className="grid">
        {sel && <WorkerDetail id={sel} onClose={() => setSel(null)} onChanged={depts.reload} />}
        <Card title={`Organization: ${depts.data?.length ?? 0} departments, ${total} roles (${impl} working)`} span={12}
          right={<span className="notice">Click any working role to see what it's doing.</span>}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(300px,1fr))", gap: 12 }}>
            {(depts.data ?? []).map((d) => (
              <div className="dept" key={d.id}>
                <div className="dept-head"><b>{d.name}</b><Badge v={d.status} /></div>
                <div className="notice" style={{ marginBottom: 6 }}>{d.implemented}/{d.roles} roles working</div>
                <div className="roles">
                  {d.workers.map((w: any) => (
                    <div key={w.id} className={`role ${w.implemented ? "" : "planned"} ${sel === w.id ? "sel" : ""}`}
                      onClick={() => w.implemented && (setSel(w.id), window.scrollTo({ top: 0, behavior: "smooth" }))}>
                      <span><span className={`dot ${DOT[w.health] ?? ""}`} />{w.name}</span>
                      <Badge v={w.health} />
                    </div>))}
                </div>
              </div>))}
          </div>
        </Card>

        <Card title="Event timeline" span={8}>
          {!events.data?.length ? <Empty>No events.</Empty> : (
            <table><thead><tr><th>#</th><th>When</th><th>Type</th><th>Producer</th><th>Delivered</th></tr></thead>
              <tbody>{events.data.map((e) => (
                <tr key={e.id} title={JSON.stringify(e.payload)}><td className="num">{e.seq}</td><td>{fmtTime(e.occurred_at)}</td><td className="mono">{e.event_type} v{e.schema_version}</td>
                  <td className="mono">{e.producer}</td><td>{e.published_at ? <Badge v="RELAYED" title="relayed to Redis Streams" /> : <Badge v="OUTBOX" title="waiting in Postgres outbox" />}</td></tr>))}</tbody></table>)}
        </Card>
        <Card title="Dead-letter queue" span={4}>
          {!dlq.data?.length ? <Empty>Empty. No events failed past their retry budget.</Empty> : (
            <table><tbody>{dlq.data.map((d) => (
              <tr key={d.id}><td>{fmtTime(d.created_at)}<div className="mono">{d.consumer}</div></td><td className="mono">{d.event_type}<pre>{d.error}</pre></td></tr>))}</tbody></table>)}
        </Card>
      </div>
    </Shell>
  );
}
