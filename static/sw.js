/* SmartServe V10 service worker.
 *
 * Deliberately conservative: the app is data-heavy and account-specific, so we
 * only precache the offline shell and static assets. API responses, pages and
 * uploads are always fetched from the network first — no stale "booked" state
 * and no private data left in the cache.
 */
const VERSION = "smartserve-v10-1";
const STATIC_ASSETS = [
  "/offline",
  "/static/css/v10.css",
  "/static/js/v10.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache account data or APIs.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/socket.io/")) return;

  // Static assets: cache-first.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(VERSION).then((cache) => cache.put(request, copy));
        return response;
      }).catch(() => cached))
    );
    return;
  }

  // Pages: network-first, fall back to the offline shell.
  if (request.mode === "navigate" || request.headers.get("accept")?.includes("text/html")) {
    event.respondWith(
      fetch(request).catch(() => caches.match("/offline").then((cached) => cached || caches.match(request)))
    );
  }
});
