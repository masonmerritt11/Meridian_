// MERIDIAN service worker: makes the site installable. Network-only on purpose:
// prices, positions and account figures are never cached on the device.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0b0e13;color:#d7dde6;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0"><div style="text-align:center"><b>MERIDIAN is offline</b><p style="color:#7d8797">Check your connection and reopen the app.</p></div>',
    { headers: { "Content-Type": "text/html" } })));
});

// Phone alerts: new trade recommendations and exit advisories (sent by the server; nothing else).
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: "MERIDIAN", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "MERIDIAN", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag,
    icon: "/icon-192.png", badge: "/icon-192.png", data: { url: d.url || "/" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) { if ("focus" in w) { w.navigate(url).catch(() => {}); return w.focus(); } }
    return self.clients.openWindow(url);
  }));
});
