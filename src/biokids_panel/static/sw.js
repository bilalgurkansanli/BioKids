/* The panel's service worker.
 *
 * It caches the *shell* and nothing else (D-34, UX.md §3.5): the stylesheet, the
 * fonts, the icons and this script. Live data is never cached — a parent must
 * never be shown a cheerful "42 minutes left" that was true an hour ago, because
 * the whole value of the Today screen is that it is now.
 *
 * Every navigation goes to the network first. When the network is not there the
 * parent gets /offline from the cache: one sentence saying the panel could not
 * be reached, with nothing about the family in it (D-194). It used to be the
 * stylesheet, served as the page.
 */
// Replaced when served (biokids_panel.app.service_worker) with a name that
// changes whenever the cached files do, so an upgrade reaches the phone.
const CACHE = "biokids-shell-v1";

const SHELL = [
  "/static/shared/tokens.css",
  "/static/panel.css",
  "/static/live.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.webmanifest",
  "/offline",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      // A missing shell file must not stop the worker installing; the page
      // still works straight from the network.
      .catch(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never the event stream.
  if (url.pathname === "/events") return;

  if (SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request)),
    );
    return;
  }

  // A page: always the network, and the offline page only when there is no
  // network. Nothing that describes a child is ever put in the cache.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/offline").then((hit) => hit || Response.error()),
      ),
    );
  }
});
