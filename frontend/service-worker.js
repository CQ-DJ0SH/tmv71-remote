/* TM-V71 Remote — app-shell service worker.
 * Caches only the static UI shell so the app launches instantly and survives a
 * flaky link. It never caches the API or WebSocket, and never touches requests
 * to a different origin (so a configured remote API backend keeps working). */
"use strict";

const CACHE = "tmv71-shell-v13";
const SHELL = [
  "./",
  "index.html",
  "styles.css",
  "app.js",
  "manifest.webmanifest",
  "favicon.svg",
  "fonts/fonts.css",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "icons/apple-touch-icon.png",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      // addAll fails the whole install if any URL 404s — add individually so a
      // missing optional asset can't brick the install.
      .then(c => Promise.allSettled(SHELL.map(u => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  const url = new URL(req.url);
  // Only same-origin GET requests for the static shell are our business.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  // Never touch the live API or WebSocket upgrades.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) return;

  // Network-first so a deployed UI update is always picked up when online; the
  // cache is refreshed on every success and only used as an offline fallback
  // (for navigations, fall back to the cached app shell).
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then(hit =>
      hit || (req.mode === "navigate" ? caches.match("index.html") : Response.error())
    ))
  );
});
