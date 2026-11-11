"use client";
import { useState } from "react";
import { post } from "@/lib/api";

export default function Login() {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <form onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setErr(null);
        try { await post("/auth/login", { username: u, password: p }); location.href = "/"; }
        catch (x: any) { setErr(x.message); } finally { setBusy(false); }
      }}>
        <div className="brand" style={{ padding: 0 }}>
          <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.6" /><line x1="12" y1="1.5" x2="12" y2="22.5" stroke="currentColor" strokeWidth="1.6" /></svg>
          <span>MERIDIAN</span>
        </div>
        <label>Username<input autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} required /></label>
        {err && <div className="error">{err}</div>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <div className="notice">Advisory-only research platform. No order execution.</div>
      </form>
    </div>
  );
}
