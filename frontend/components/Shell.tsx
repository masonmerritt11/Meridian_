"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api, ago, post, usePoll } from "@/lib/api";

const NAV = [
  ["/", "Executive Overview"],
  ["/radar/", "Opportunity Radar"],
  ["/operations/", "Department Operations"],
  ["/risk/", "Risk Command"],
  ["/lab/", "Research Laboratory"],
  ["/archive/", "Decision Archive"],
];

export default function Shell({ title, children }: { title: string; children: ReactNode }) {
  const path = usePathname();
  const [me, setMe] = useState<{ username: string; role: string } | null>(null);
  const ov = usePoll<any>("/overview", 15000);
  useEffect(() => { api("/auth/me").then(setMe).catch(() => {}); }, []);
  const sim = ov.data?.simulated;
  return (
    <div className="shell">
      <aside className="nav">
        <div className="brand">
          <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.6" /><line x1="12" y1="1.5" x2="12" y2="22.5" stroke="currentColor" strokeWidth="1.6" /></svg>
          <span>MERIDIAN</span>
        </div>
        <nav>
          {NAV.map(([href, label]) => (
            <Link key={href} href={href} className={path === href || (href !== "/" && path?.startsWith(href)) ? "active" : ""}>{label}</Link>
          ))}
        </nav>
        <div className="nav-foot">
          <div className="advisory">Advisory only. MERIDIAN never places, modifies or cancels orders.</div>
          {me && (
            <div className="who">
              {me.username} · {me.role}
              <button className="link" onClick={() => post("/auth/logout", {}).then(() => (location.href = "/login/"))}>Sign out</button>
            </div>
          )}
        </div>
      </aside>
      <main>
        {sim && (
          <div className="sim-banner">
            SIMULATED DATA. Prices, signals and memos built on it are demonstrations, not market information.
          </div>
        )}
        <div className="topbar">
          <h1>{title}</h1>
          <div className="topbar-meta">
            <span>mode: <b>{ov.data?.mode ?? "…"}</b></span>
            <span>workers: <b>{ov.data?.workers_running ? "running" : "not running"}</b></span>
            <span>refreshed {ov.updated ? ago(ov.updated.toISOString()) : "…"}</span>
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}
