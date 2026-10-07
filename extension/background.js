// BioKids: the extension the managed policy installs in the children's Chromium.
//
// It does two things and nothing else:
//
//   1. When the filter closes a page the child tried to open, the tab shows the
//      launcher's own "this site is closed" page instead of a Chromium error.
//   2. The child's launcher hears the *name* of each site the child opens: the
//      host, never the path, the query, the title or anything on the page, and
//      at most once per host every ten minutes. docs/SECURITY.md section 7 is
//      the boundary this file must stay inside.
//
// No content scripts, no access to any page, no remote code, and no network
// peer other than the child's own launcher on 127.0.0.1.

"use strict";

// ------------------------------------------------------------- the launcher

// Each child's launcher listens on 127.0.0.1:(7879 + uid % 100), and nftables
// lets a child's processes reach only their own port. The extension cannot know
// the uid, so it asks every port in the range and keeps the one that answers.
const PROBE_FIRST_PORT = 7879;
const PROBE_LAST_PORT = 7978;
const PROBE_TIMEOUT_MS = 1500;
// After a probe that found nothing, wait this long before probing again.
const PROBE_BACKOFF_MS = 15 * 1000;

// The healthz contract: GET <origin>/healthz answers JSON, and the launcher's
// body carries {"biokids": "launcher", ...}. Nothing else counts.
const HEALTHZ_PATH = "/healthz";
const HEALTHZ_KEY = "biokids";
const HEALTHZ_VALUE = "launcher";

const LAUNCHER_HOST = "127.0.0.1";
const BLOCKED_PATH = "/blocked";
const VISIT_PATH = "/visit";

// ---------------------------------------------------------------- behaviour

// A top-level navigation that failed with one of these was closed by BioKids.
const BLOCK_ERRORS = new Set([
  "net::ERR_BLOCKED_BY_ADMINISTRATOR", // URLBlocklist in the managed policy
  "net::ERR_NAME_NOT_RESOLVED", // the children's DNS filter answers NXDOMAIN
]);

// One report per host per this long. Kept in chrome.storage.session, so a
// service worker that Chromium stopped and started again does not resend.
const VISIT_INTERVAL_MS = 10 * 60 * 1000;

const STORAGE_LAUNCHER = "launcher";
const STORAGE_VISITS = "visits";

// The webNavigation events are only delivered for these schemes.
const WEB_ONLY = { url: [{ schemes: ["http", "https"] }] };

// ------------------------------------------------------------------ helpers

// The host of an http(s) URL on another machine, or "" for anything else:
// another scheme, an unparsable URL, or this machine (the launcher included).
function webHost(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return "";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "";
  }
  const host = parsed.hostname.replace(/\.$/, "");
  if (
    host === "" ||
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "[::1]" ||
    /^127\./.test(host)
  ) {
    return "";
  }
  return host;
}

// True when `origin` answers /healthz as a BioKids launcher.
async function isLauncher(origin, signal) {
  try {
    const response = await fetch(origin + HEALTHZ_PATH, {
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal,
    });
    if (!response.ok) {
      return false;
    }
    const body = await response.json();
    return body !== null && typeof body === "object" && body[HEALTHZ_KEY] === HEALTHZ_VALUE;
  } catch {
    return false;
  }
}

// Ask every port in the range at once; the first launcher to answer wins and
// the remaining requests are cancelled. "" when none answered in time.
async function probe() {
  const stop = new AbortController();
  const attempts = [];
  for (let port = PROBE_FIRST_PORT; port <= PROBE_LAST_PORT; port += 1) {
    const origin = `http://${LAUNCHER_HOST}:${port}`;
    const signal = AbortSignal.any([stop.signal, AbortSignal.timeout(PROBE_TIMEOUT_MS)]);
    attempts.push(
      isLauncher(origin, signal).then((found) => {
        if (!found) {
          throw new Error(`no launcher on ${origin}`);
        }
        return origin;
      }),
    );
  }
  try {
    return await Promise.any(attempts);
  } catch {
    return "";
  } finally {
    stop.abort();
  }
}

let probing = null;
let lastEmptyProbe = 0;

// One probe at a time, shared by everyone who asks while it runs; the result
// is cached for the rest of the browser session.
function probeOnce() {
  if (probing === null) {
    probing = (async () => {
      if (Date.now() - lastEmptyProbe < PROBE_BACKOFF_MS) {
        return "";
      }
      const origin = await probe();
      if (origin) {
        await chrome.storage.session.set({ [STORAGE_LAUNCHER]: origin });
      } else {
        lastEmptyProbe = Date.now();
      }
      return origin;
    })().finally(() => {
      probing = null;
    });
  }
  return probing;
}

// The launcher's origin, such as "http://127.0.0.1:7880", or "" when there is
// none. With `verify`, a cached origin is asked /healthz again before use.
async function findLauncher({ verify = false } = {}) {
  const stored = await chrome.storage.session.get(STORAGE_LAUNCHER);
  const cached = stored[STORAGE_LAUNCHER];
  if (typeof cached === "string" && cached !== "") {
    if (!verify || (await isLauncher(cached, AbortSignal.timeout(PROBE_TIMEOUT_MS)))) {
      return cached;
    }
    await forgetLauncher(cached);
  }
  return probeOnce();
}

async function forgetLauncher(origin) {
  const stored = await chrome.storage.session.get(STORAGE_LAUNCHER);
  if (stored[STORAGE_LAUNCHER] === origin) {
    await chrome.storage.session.remove(STORAGE_LAUNCHER);
  }
}

// ---------------------------------------------------------- the block page

async function showBlockedPage(tabId, url) {
  const origin = await findLauncher({ verify: true });
  if (!origin) {
    // No launcher to ask: Chromium's own error page stays, which still
    // shows nothing of the closed site.
    console.warn("BioKids: no launcher answered; leaving Chromium's error page");
    return;
  }
  const target = `${origin}${BLOCKED_PATH}?url=${encodeURIComponent(url)}`;
  try {
    await chrome.tabs.update(tabId, { url: target });
  } catch (error) {
    // The tab was closed in the meantime.
    console.warn("BioKids: could not show the block page:", error);
  }
}

chrome.webNavigation.onErrorOccurred.addListener((details) => {
  if (details.frameId !== 0 || details.tabId < 0 || !BLOCK_ERRORS.has(details.error)) {
    return;
  }
  if (!webHost(details.url)) {
    return;
  }
  showBlockedPage(details.tabId, details.url);
}, WEB_ONLY);

// --------------------------------------------------------- the visit report

// Sends the host and nothing else. True when the launcher accepted it.
async function sendVisit(host) {
  let origin = await findLauncher();
  for (let attempt = 0; origin && attempt < 2; attempt += 1) {
    try {
      const response = await fetch(origin + VISIT_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host }),
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      });
      return response.ok;
    } catch {
      // The launcher moved or restarted: find it again, once.
      await forgetLauncher(origin);
      origin = attempt === 0 ? await findLauncher() : "";
    }
  }
  return false;
}

async function reportVisit(host) {
  const now = Date.now();
  const stored = await chrome.storage.session.get(STORAGE_VISITS);
  const visits = stored[STORAGE_VISITS] || {};
  const recent = (at) => typeof at === "number" && now - at >= 0 && now - at < VISIT_INTERVAL_MS;
  if (recent(visits[host])) {
    return;
  }
  if (!(await sendVisit(host))) {
    return;
  }
  // Only the last ten minutes are worth remembering.
  const kept = {};
  for (const [name, at] of Object.entries(visits)) {
    if (recent(at)) {
      kept[name] = at;
    }
  }
  kept[host] = now;
  await chrome.storage.session.set({ [STORAGE_VISITS]: kept });
}

// Reports run one after another, so two tabs opening the same site at once
// read and write the map in turn and the host is reported once.
let queue = Promise.resolve();

function enqueue(task) {
  queue = queue.then(task).catch((error) => {
    console.warn("BioKids: the visit was not reported:", error);
  });
}

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) {
    return;
  }
  const host = webHost(details.url);
  if (!host) {
    return;
  }
  enqueue(() => reportVisit(host));
}, WEB_ONLY);
