"use client";
import type { ReactNode } from "react";

const TONE: Record<string, string> = {
  HEALTHY: "ok", FRESH: "ok", ALLOW: "ok", RECOMMEND: "ok", RECOMMENDED: "ok", HOLD: "ok", ACTIVE: "ok", ACCEPT: "ok", LONG: "ok", SUPPORTS: "ok",
  RESTRICT: "warn", DEGRADED: "warn", STALE: "warn", DEFER: "warn", DEFERRED: "warn", REVIEW: "warn", UNKNOWN: "warn", NEUTRAL: "muted",
  BLOCK: "bad", BLOCKED: "bad", FAILING: "bad", EXIT: "bad", UNAVAILABLE: "bad", REJECT: "bad", REJECTED: "bad", OPPOSES: "bad", SHORT: "bad", DECLINE: "bad",
  PLANNED: "muted", SUCCEEDED: "ok", RELAYED: "ok", MATCH: "ok", FAILED: "bad", MISMATCH: "bad", OUTBOX: "warn", RUNNING: "info", PENDING: "muted", INACTIVE: "muted", CLOSED: "muted", EXPIRED: "muted", INVALIDATED: "muted", SIMULATED: "sim",
};

/** Machine codes (RISK_REVIEW) shown as readable words (RISK REVIEW); the raw code stays in the tooltip. */
export const human = (code: string) => code.replace(/_/g, " ");

export function Code({ c }: { c: string }) {
  return <span className="code" title={c}>{human(c)}</span>;
}

export function Badge({ v, title }: { v?: string | null; title?: string }) {
  if (!v) return <span className="badge muted">—</span>;
  return <span className={`badge ${TONE[v] ?? "info"}`} title={title ?? v}>{human(v)}</span>;
}

export function Card({ title, right, children, span }: { title: string; right?: ReactNode; children: ReactNode; span?: number }) {
  return (
    <section className="card" style={span ? { gridColumn: `span ${span}` } : undefined}>
      <header><h2>{title}</h2>{right}</header>
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error">Backend error: {error}</div> : null;
}

export function Meter({ value, max, label }: { value: number; max: number; label?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const tone = pct >= 100 ? "bad" : pct >= 70 ? "warn" : "ok";
  return (
    <div className="meter" title={label}>
      <div className={`meter-fill ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Sparkline({ points, width = 640, height = 160, marks = [] }:
  { points: number[]; width?: number; height?: number; marks?: { y: number; label: string; tone: string }[] }) {
  if (points.length < 2) return <Empty>No candles stored for this instrument yet.</Empty>;
  const all = [...points, ...marks.map((m) => m.y)];
  const lo = Math.min(...all), hi = Math.max(...all), pad = (hi - lo) * 0.05 || 1;
  const y = (v: number) => height - ((v - (lo - pad)) / (hi - lo + 2 * pad)) * height;
  const x = (i: number) => (i / (points.length - 1)) * (width - 70);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="spark" role="img" aria-label="price chart">
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.6" />
      {marks.map((m) => (
        <g key={m.label}>
          <line x1={0} x2={width - 70} y1={y(m.y)} y2={y(m.y)} className={`mark ${m.tone}`} />
          <text x={width - 66} y={y(m.y) + 4} className={`mark-label ${m.tone}`}>{m.label}</text>
        </g>
      ))}
    </svg>
  );
}
