"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

function b64ToBytes(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

type State = "loading" | "unsupported" | "install-first" | "blocked" | "off" | "on";

/** Phone alerts for new trade recommendations and exit advisories. */
export default function PushToggle() {
  const [state, setState] = useState<State>("loading");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
    const standalone = matchMedia("(display-mode: standalone)").matches || (navigator as any).standalone === true;
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      setState(ios && !standalone ? "install-first" : "unsupported");
      return;
    }
    if (Notification.permission === "denied") { setState("blocked"); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) { setState("off"); return; }
    const st = await api<any>(`/push/status?endpoint=${encodeURIComponent(sub.endpoint)}`).catch(() => null);
    if (st && !st.this_device) {  // server lost it (or another login): re-register this phone
      const j = sub.toJSON() as any;
      await post("/push/subscribe", { endpoint: j.endpoint, keys: j.keys }).catch(() => {});
    }
    setState("on");
  }
  useEffect(() => { refresh().catch(() => setState("unsupported")); }, []);

  async function turnOn() {
    setBusy(true); setMsg(null);
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { setState(perm === "denied" ? "blocked" : "off"); return; }
      const st = await api<any>("/push/status");
      const reg = await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ??
        (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(st.public_key) as BufferSource }));
      const j = sub.toJSON() as any;
      await post("/push/subscribe", { endpoint: j.endpoint, keys: j.keys });
      setState("on");
      await post("/push/test", {}).then(() => setMsg("Test alert sent.")).catch((e) => setMsg(e.message));
    } catch (e: any) {
      setMsg(e?.message ?? String(e));
    } finally { setBusy(false); }
  }

  async function turnOff() {
    setBusy(true); setMsg(null);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) { await post("/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
      setState("off");
    } finally { setBusy(false); }
  }

  async function test() {
    setBusy(true); setMsg(null);
    await post("/push/test", {}).then(() => setMsg("Test alert sent.")).catch((e) => setMsg(e.message));
    setBusy(false);
  }

  if (state === "loading") return null;
  return (
    <div className="push">
      <div className="push-head">Phone alerts {state === "on" && <span className="badge ok">ON</span>}</div>
      <div className="push-sub">New trade recommendations and exit advisories.</div>
      {state === "install-first" && <div className="push-sub">On iPhone, add MERIDIAN to your Home Screen first (Share → Add to Home Screen), open it from there, then turn alerts on.</div>}
      {state === "unsupported" && <div className="push-sub">This browser can't receive alerts.</div>}
      {state === "blocked" && <div className="push-sub">Notifications are blocked for this site. Allow them in your phone's settings, then reopen MERIDIAN.</div>}
      {state === "off" && <button className="primary" disabled={busy} onClick={turnOn}>Turn on alerts</button>}
      {state === "on" && (
        <div className="push-actions">
          <button disabled={busy} onClick={test}>Send test</button>
          <button className="link" disabled={busy} onClick={turnOff}>Turn off on this device</button>
        </div>
      )}
      {msg && <div className="push-sub">{msg}</div>}
    </div>
  );
}
