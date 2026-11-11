"use client";
import Shell from "@/components/Shell";
import { Badge, Card, Empty, ErrorNote } from "@/components/ui";
import { fmtTime, usd, usePoll } from "@/lib/api";

export default function Archive() {
  const arc = usePoll<any>("/archive", 30000);
  const audit = usePoll<any>("/audit?limit=150", 30000);
  return (
    <Shell title="Decision Archive">
      <ErrorNote error={arc.error || audit.error} />
      <div className="grid">
        <Card title="Recommendation history" span={12}>
          {!arc.data?.memos?.length ? <Empty>No committee memos yet.</Empty> : (
            <table><thead><tr><th>Issued</th><th>Committee</th><th>Trade</th><th>Max loss</th><th>Risk</th><th>CIO decision</th><th>Source</th></tr></thead>
              <tbody>{arc.data.memos.map((m: any) => (
                <tr key={m.id} className="click" onClick={() => (location.href = `/lab/?id=${m.case_id}`)}>
                  <td>{fmtTime(m.created_at)}</td><td><Badge v={m.recommendation} /> <span className="notice">score {Number(m.score).toFixed(1)}</span></td>
                  <td><Badge v={m.trade?.direction} /> <span className="mono">{m.trade?.instrument_id}</span> <span className="notice">{m.trade?.entry_reference} / {m.trade?.stop}</span></td>
                  <td className="num">{usd(m.trade?.max_loss_usd)}</td>
                  <td><Badge v={m.risk?.outcome} /></td>
                  <td>{m.cio_decision ? <><Badge v={m.cio_decision.decision} /> <span className="notice">{fmtTime(m.cio_decision.created_at)} {m.cio_decision.note}</span></> : <span className="notice">no decision</span>}</td>
                  <td><Badge v={m.data_source} /></td>
                </tr>))}</tbody></table>)}
        </Card>
        <Card title="Audit log" span={12} right={audit.data && <span>hash chain <Badge v={audit.data.chain_valid ? "MATCH" : "MISMATCH"} title={audit.data.chain_valid ? "verified" : `broken at ${audit.data.first_bad_id}`} /></span>}>
          {!audit.data?.entries?.length ? <Empty>No audit entries.</Empty> : (
            <table><thead><tr><th>#</th><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th><th>Hash</th></tr></thead>
              <tbody>{audit.data.entries.map((a: any) => (
                <tr key={a.id}><td className="num">{a.id}</td><td>{fmtTime(a.ts)}</td><td className="mono">{a.actor}</td><td className="mono">{a.action}</td>
                  <td className="mono notice">{a.entity_type}:{String(a.entity_id).slice(0, 8)}</td>
                  <td><pre>{JSON.stringify(a.details).slice(0, 160)}</pre></td><td className="mono notice">{a.hash.slice(0, 10)}</td></tr>))}</tbody></table>)}
        </Card>
      </div>
    </Shell>
  );
}
