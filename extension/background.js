// BioKids: the extension the managed policy installs in the children's Chromium.
//
// It does three things and nothing else:
//
//   1. When the filter closes a page the child tried to open, the tab shows the
//      launcher's own "this site is closed" page instead of a Chromium error.
//   2. The child's launcher hears the *name* of each site the child opens: the
//      host, never the path, the query, the title or anything on the page, and
//      at most once per host every ten minutes. docs/SECURITY.md section 7 is
//      the boundary this file must stay inside.
//   3. A search for a word on BioKids' list is never sent (D-202). The launcher
//      serves the list as regular expressions; Chromium matches them against
//      each request (declarativeNetRequest) and refuses it, and the tab shows
//      the launcher's "we can't search that" page. Nothing about the search is
//      reported to anyone: not the word, not that it happened.
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
// Long enough for a busy machine: the ports that are not a launcher refuse at
// once, so only the launcher's own answer is waited for. At 1.5 s a VM still
// starting the browser missed it, and the child was left on Chromium's
// "blocked by an extension" page instead of the launcher's.
const PROBE_TIMEOUT_MS = 5000;
// After a probe that found nothing, wait this long before probing again.
const PROBE_BACKOFF_MS = 15 * 1000;
// A block page waiting for the launcher asks again after these pauses, past
// the back-off: a child is looking at the error page meanwhile.
const BLOCK_PAGE_RETRY_MS = [1000, 3000, 6000];

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

// ------------------------------------------------------------ search rules

// GET <launcher>/search-rules answers {"biokids": "search-rules", "version",
// "complete", "patterns": [RE2 expressions]}. The module search_filter in
// biokids_core builds and tests them; docs/SECURITY.md section 5 says what
// they match.
const SEARCH_RULES_PATH = "/search-rules";
const SEARCH_RULES_KEY = "search-rules";
const SEARCH_RULES_VALUE = "search-rules";
// Asked again this often, and whenever the launcher answers after it did not.
const SEARCH_RULES_ALARM = "search-rules";
const SEARCH_RULES_MINUTES = 5;
// A navigation Chromium refused because of a rule fails with this error. No
// other extension can exist (ExtensionInstallBlocklist "*"), so it is ours.
const SEARCH_BLOCKED_ERROR = "net::ERR_BLOCKED_BY_CLIENT";
const SEARCH_BLOCKED_REASON = "search.blocked";
// Rule 1 lets the launcher's own pages through, whatever is in their address;
// the patterns are rules 2 and up. Chromium takes at most 1000 regex rules.
const LAUNCHER_RULE_ID = 1;
const FIRST_PATTERN_RULE_ID = 2;
const MAX_PATTERNS = 990;
const MAX_PATTERN_LENGTH = 4000;
// Pages, frames (the offline Wikipedia shows articles in one), and the
// suggestions a search box fetches while the child types.
const SEARCHED_TYPES = ["main_frame", "sub_frame", "xmlhttprequest", "script"];

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
function probeOnce({ urgent = false } = {}) {
  if (probing === null) {
    probing = (async () => {
      if (!urgent && Date.now() - lastEmptyProbe < PROBE_BACKOFF_MS) {
        return "";
      }
      const origin = await probe();
      if (origin) {
        await chrome.storage.session.set({ [STORAGE_LAUNCHER]: origin });
        if (searchRulesStale) {
          // The launcher answers again: fetch what it could not give before.
          refreshSearchRules();
        }
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
// `urgent` probes even inside the back-off, for a child waiting on a block page.
async function findLauncher({ verify = false, urgent = false } = {}) {
  const stored = await chrome.storage.session.get(STORAGE_LAUNCHER);
  const cached = stored[STORAGE_LAUNCHER];
  if (typeof cached === "string" && cached !== "") {
    if (!verify || (await isLauncher(cached, AbortSignal.timeout(PROBE_TIMEOUT_MS)))) {
      return cached;
    }
    await forgetLauncher(cached);
  }
  return probeOnce({ urgent });
}

async function forgetLauncher(origin) {
  const stored = await chrome.storage.session.get(STORAGE_LAUNCHER);
  if (stored[STORAGE_LAUNCHER] === origin) {
    await chrome.storage.session.remove(STORAGE_LAUNCHER);
  }
}

// ---------------------------------------------------------- the block page

// The launcher's page for a closed site, given its address, or for a refused
// search, given only the reason: the words of a search never leave the browser.
async function showBlockedPage(tabId, url, reason = "") {
  let origin = await findLauncher({ verify: true });
  for (const pause of BLOCK_PAGE_RETRY_MS) {
    if (origin) {
      break;
    }
    // The launcher may be slow to answer while the browser is still starting.
    await new Promise((resolve) => setTimeout(resolve, pause));
    origin = await findLauncher({ urgent: true });
  }
  if (!origin) {
    // No launcher to ask: Chromium's own error page stays, which still
    // shows nothing of the closed site.
    console.warn("BioKids: no launcher answered; leaving Chromium's error page");
    return;
  }
  const query = reason
    ? `reason=${encodeURIComponent(reason)}`
    : `url=${encodeURIComponent(url)}`;
  try {
    await chrome.tabs.update(tabId, { url: `${origin}${BLOCKED_PATH}?${query}` });
  } catch (error) {
    // The tab was closed in the meantime.
    console.warn("BioKids: could not show the block page:", error);
  }
}

chrome.webNavigation.onErrorOccurred.addListener((details) => {
  if (details.tabId < 0) {
    return;
  }
  if (details.error === SEARCH_BLOCKED_ERROR) {
    // A search rule refused the page, or a frame in it (the offline
    // Wikipedia shows its articles in one): the whole tab says why.
    showBlockedPage(details.tabId, "", SEARCH_BLOCKED_REASON);
    return;
  }
  if (details.frameId !== 0 || !BLOCK_ERRORS.has(details.error)) {
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

// ------------------------------------------------------- the search rules

// True until the rules were fetched once in this service worker's life, and
// again after a fetch failed: the next launcher that answers is asked at once.
let searchRulesStale = true;
let refreshing = null;
// The installed patterns as JavaScript expressions, for addresses a page
// changes without a request (below); rebuilt after every install.
let compiledRules = null;

function isPattern(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_PATTERN_LENGTH &&
    /^[\x20-\x7e]+$/.test(value)
  );
}

async function fetchSearchRules(origin) {
  const response = await fetch(origin + SEARCH_RULES_PATH, {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error(`the launcher answered ${response.status}`);
  }
  const fetched = await response.json();
  if (
    fetched === null ||
    typeof fetched !== "object" ||
    fetched[HEALTHZ_KEY] !== SEARCH_RULES_VALUE ||
    typeof fetched.version !== "string" ||
    !Array.isArray(fetched.patterns)
  ) {
    throw new Error("the launcher's answer is not a set of search rules");
  }
  return fetched;
}

// The rules to install: the launcher's own pages always open (a closed site's
// address, carried to its block page, may hold a listed word), then one
// blocking rule per pattern Chromium accepts. Every action and condition is
// fixed here; the launcher supplies expressions and nothing else.
async function buildSearchRules(origin, patterns) {
  const rules = [
    {
      id: LAUNCHER_RULE_ID,
      priority: 2,
      action: { type: "allow" },
      condition: { urlFilter: `|${origin}/`, resourceTypes: SEARCHED_TYPES },
    },
  ];
  const accepted = [];
  for (const regex of patterns.filter(isPattern).slice(0, MAX_PATTERNS)) {
    const verdict = await chrome.declarativeNetRequest.isRegexSupported({
      regex,
      isCaseSensitive: false,
    });
    if (!verdict.isSupported) {
      console.error(
        `BioKids: Chromium refused a search pattern (${verdict.reason}); its words are not stopped:`,
        regex,
      );
      continue;
    }
    rules.push({
      id: FIRST_PATTERN_RULE_ID + accepted.length,
      priority: 1,
      action: { type: "block" },
      condition: {
        regexFilter: regex,
        isUrlFilterCaseSensitive: false,
        resourceTypes: SEARCHED_TYPES,
      },
    });
    accepted.push(regex);
  }
  return { rules, accepted };
}

async function installSearchRules(origin, fetched) {
  const { rules, accepted } = await buildSearchRules(origin, fetched.patterns);
  if (accepted.length === 0) {
    throw new Error("Chromium could use none of the launcher's search patterns");
  }
  const installed = await chrome.declarativeNetRequest.getDynamicRules();
  // One call, which Chromium applies whole or not at all: there is no moment
  // between the old rules and the new ones. Dynamic rules outlive a restart of
  // the browser, so the last set stays in force while the launcher is away.
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: installed.map((rule) => rule.id),
    addRules: rules,
  });
  await chrome.storage.local.set({
    [SEARCH_RULES_KEY]: {
      version: fetched.version,
      complete: fetched.complete === true,
      origin,
      patterns: accepted,
    },
  });
  compiledRules = null;
  console.info(`BioKids: ${accepted.length} search rules installed (${fetched.version})`);
}

async function storedSearchRules() {
  const stored = await chrome.storage.local.get(SEARCH_RULES_KEY);
  const value = stored[SEARCH_RULES_KEY];
  return value !== null && typeof value === "object" ? value : null;
}

async function blockingRuleCount() {
  const rules = await chrome.declarativeNetRequest.getDynamicRules();
  return rules.filter((rule) => rule.action.type === "block").length;
}

// Never silent: no rules in a child's browser means any search goes through.
// In a grown-up's profile, where no launcher ever answers, the error is
// expected, and nothing there is blocked.
async function warnIfUnprotected(why) {
  const count = await blockingRuleCount();
  if (count === 0) {
    console.error(
      `BioKids: NO search rules are installed (${why}). In a child's browser, forbidden searches are not being stopped.`,
    );
  } else {
    console.warn(
      `BioKids: the search rules were not refreshed (${why}); the ${count} installed last stay in force.`,
    );
  }
}

async function refreshOnce() {
  const origin = await findLauncher();
  if (!origin) {
    searchRulesStale = true;
    await warnIfUnprotected("no launcher answered");
    return false;
  }
  let fetched;
  try {
    fetched = await fetchSearchRules(origin);
  } catch (error) {
    searchRulesStale = true;
    await forgetLauncher(origin);
    await warnIfUnprotected(String(error));
    return false;
  }
  const stored = await storedSearchRules();
  const installed =
    stored !== null && stored.origin === origin && (await blockingRuleCount()) > 0;
  if (installed && stored.version === fetched.version) {
    searchRulesStale = fetched.complete !== true;
    return true;
  }
  if (installed && stored.complete === true && fetched.complete !== true) {
    // The launcher could not reach the daemon and knows only the default list:
    // the installed set, with the parent's own words, is the better one.
    searchRulesStale = true;
    console.warn(
      "BioKids: the launcher has only the default search list; keeping the installed rules",
    );
    return false;
  }
  await installSearchRules(origin, fetched);
  searchRulesStale = fetched.complete !== true;
  return true;
}

// One refresh at a time, shared by everyone who asks while it runs.
function refreshSearchRules() {
  if (refreshing === null) {
    refreshing = refreshOnce()
      .catch(async (error) => {
        searchRulesStale = true;
        try {
          await warnIfUnprotected(String(error));
        } catch {
          console.error("BioKids: the search rules could not be checked:", error);
        }
        return false;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

function scheduleSearchRules() {
  chrome.alarms.create(SEARCH_RULES_ALARM, { periodInMinutes: SEARCH_RULES_MINUTES });
  refreshSearchRules();
}

chrome.runtime.onInstalled.addListener(scheduleSearchRules);
chrome.runtime.onStartup.addListener(scheduleSearchRules);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SEARCH_RULES_ALARM) {
    refreshSearchRules();
  }
});

async function compiledSearchRules() {
  if (compiledRules === null) {
    const stored = await storedSearchRules();
    const patterns = stored !== null && Array.isArray(stored.patterns) ? stored.patterns : [];
    const list = [];
    for (const pattern of patterns) {
      try {
        list.push(new RegExp(pattern, "i"));
      } catch (error) {
        console.error("BioKids: a search pattern does not compile here:", error);
      }
    }
    compiledRules = { origin: stored !== null ? String(stored.origin) : "", list };
  }
  return compiledRules;
}

// Some pages search without loading a new one: YouTube sends the words in the
// body of a request, which no rule can read, then changes the address bar to
// the results' address. That address is checked here against the same
// expressions, and the tab goes to the same page.
chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
  if (details.tabId < 0 || details.frameId !== 0) {
    return;
  }
  const { origin, list } = await compiledSearchRules();
  if (origin && details.url.startsWith(`${origin}/`)) {
    return;
  }
  if (list.some((pattern) => pattern.test(details.url))) {
    showBlockedPage(details.tabId, "", SEARCH_BLOCKED_REASON);
  }
}, WEB_ONLY);
