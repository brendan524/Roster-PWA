/* AB Group service worker.
   - App shell: network-first, falls back to cache so the app opens offline.
   - Fonts/icons: cache-first (they never change without a version bump).
   - /api/auth/me and /api/roster/me: network-first; if offline, serve the last copy
     marked with x-ab-offline so the app can say so. Cleared on logout.
   - Everything else under /api (logins, admin data) is never cached. */

const VERSION = "v14";
const SHELL = `ab-shell-${VERSION}`;
const DATA = "ab-data"; // personal roster cache; wiped on logout
const SHELL_FILES = ["/", "/app.css", "/app.js", "/manifest.webmanifest",
  "/fonts/lato-400.woff2", "/fonts/lato-700.woff2", "/fonts/lato-900.woff2",
  "/icons/icon-192.png", "/icons/favicon-32.png", "/logo.png",
  "/clock-shared.js", "/kiosk.html", "/kiosk.js", "/kiosk.css", "/kiosk.webmanifest"];
const OFFLINE_API = ["/api/auth/me", "/api/roster/me", "/api/clock/me"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("ab-shell-") && k !== SHELL) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "logout") e.waitUntil(caches.delete(DATA));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    if (OFFLINE_API.includes(url.pathname)) e.respondWith(apiNetworkFirst(req, url));
    return;
  }
  if (url.pathname.startsWith("/fonts/") || url.pathname.startsWith("/icons/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
    return;
  }
  e.respondWith(shellNetworkFirst(req));
});

async function shellNetworkFirst(req) {
  // Pages are cached by path (so /kiosk.html and / each come back offline as themselves).
  const key = req.mode === "navigate" ? new URL(req.url).pathname : req;
  try {
    const res = await fetch(req);
    if (res.ok && !res.redirected) (await caches.open(SHELL)).put(key, res.clone());
    return res;
  } catch {
    return (await caches.match(key)) || (await caches.match("/")) || Response.error();
  }
}

async function apiNetworkFirst(req, url) {
  const key = url.pathname; // ignore ?refresh=true
  try {
    const res = await fetch(req);
    const cache = await caches.open(DATA);
    if (res.ok) await cache.put(key, res.clone());
    else if (res.status === 401 || res.status === 403) await caches.delete(DATA);
    return res;
  } catch {
    const hit = await (await caches.open(DATA)).match(key);
    if (!hit) return Response.error();
    const headers = new Headers(hit.headers);
    headers.set("x-ab-offline", "1");
    return new Response(await hit.blob(), { status: 200, headers });
  }
}
