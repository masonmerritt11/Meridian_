"use client";
import { useState } from "react";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote } from "@/components/ui";
import { ago, fmtTime, post, usePoll } from "@/lib/api";

const DOT: Record<string, string> = { HEALTHY: "ok", DEGRADED: "warn", UNKNOWN: "warn", STALE: "bad", FAILING: "bad" };

export default function Operations() {
  const depts = usePoll<any[]>("/departments", 15000);
  const events = usePoll<any[]>("/events?limit=60", 10000);
  const dlq = usePoll<any[]>("/events/dlq", 30000);
  const [sel, setSel] = useState<any | null>(null);
  const runs = usePoll<any[]>(sel ? `/workers/${sel.id}/runs?limit=20` : null, 15000);
  const [msg, setMsg] = useState<string | null>(null);

  return (
    <Shell title="Department Operations">
      <ErrorNote error={depts.error} />
      <div className="grid">
        <Card title="Organization — 9 departments, 52 roles" span={12} right={<span className="notice">Roles marked PLANNED are defined but have no implementation yet.</span>}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(300px,1fr))", gap: 12 }}>
            {(depts.data ?? []).map((d) => (
              <div className="dept" key={d.id}>
                <div className="dept-head"><b>{d.name}</b><Badge v={d.status} /></div>
                <div className="notice" style={{ marginBottom: 6 }}>{d.implemented}/{d.roles} roles implemented</div>
                <div className="roles">
                  {d.workers.map((w: any) => (
                    <div key={w.id} className={`role ${w.implemented ? "" : "planned"}`} onClick={() => w.implemented && setSel(w)}>
                      <span><span className={`dot ${DOT[w.health] ?? ""}`} />{w.name}</span>
                      <Badge v={w.health} />
                    </div>))}
                </div>
              </div>))}
          </div>
        </Card>

        {sel && (
          <Card title={`Worker · ${sel.id}`} span={12} right={<button className="link" onClick={() => setSel(null)}>Close</button>}>
            <div className="split">
              <dl className="kv">
                <dt>Responsibility</dt><dd>{sel.responsibility}</dd>
                <dt>Version</dt><dd>{sel.worker_version} · config v{sel.config_version}</dd>
                <dt>Schedule</dt><dd>{sel.schedule_seconds ? `every ${sel.schedule_seconds}s` : "event-driven"}</dd>
                <dt>Subscribes</dt><dd>{sel.subscribes?.join(", ") || "—"}</dd>
                <dt>Escalates to</dt><dd>{sel.escalation_to}</dd>
                <dt>Status</dt><dd><Badge v={sel.status} /> <Badge v={sel.health} /> last seen {ago(sel.last_seen)}</dd>
                <dt>Config</dt><dd><pre>{JSON.stringify(sel.config, null, 1)}</pre></dd>
              </dl>
              <div>
                <div className="form-row" style={{ marginBottom: 10 }}>
                  <button onClick={async () => {
                    try { const r = await post(`/workers/${sel.id}/status`, { status: sel.status === "ACTIVE" ? "INACTIVE" : "ACTIVE" }); setSel({ ...sel, status: r.status }); setMsg(`now ${r.status}`); depts.reload(); }
                    catch (e: any) { setMsg(e.message); }
                  }}>{sel.status === "ACTIVE" ? "Deactivate" : "Activate"}</button>
                  <span className="notice">{msg}</span>
                </div>
                <details><summary className="notice">Input / output schema</summary><pre>{JSON.stringify({ input: sel.input_schema, output: sel.output_schema }, null, 1)}</pre></details>
              </div>
            </div>
            <h3>Recent executions</h3>
            {!runs.data?.length ? <Empty>No executions recorded.</Empty> : (
              <table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Attempts</th><th>Duration</th><th>Output</th><th>Inputs</th></tr></thead>
                <tbody>{runs.data.map((r) => (
                  <tr key={r.id}><td>{fmtTime(r.started_at)}</td><td>{r.trigger}</td><td><Badge v={r.status} /></td>
                    <td className="num">{r.attempts}</td><td className="num">{r.metrics?.duration_ms} ms</td>
                    <td>{r.output?.summary}{r.error && <pre style={{ color: "var(--bad)" }}>{r.error.split("\n").slice(-2).join("\n")}</pre>}</td>
                    <td className="notice">{r.input_refs?.length ?? 0} ref(s)</td></tr>))}</tbody></table>)}
          </Card>
        )}

        <Card title="Event timeline" span={8}>
          {!events.data?.length ? <Empty>No events.</Empty> : (
            <table><thead><tr><th>Seq</th><th>When</th><th>Type</th><th>Producer</th><th>Published</th></tr></thead>
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
