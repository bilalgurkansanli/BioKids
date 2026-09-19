/* Live counters on the Today screen.
 *
 * Pure enhancement. The page is already correct when this file never loads —
 * the numbers are simply the ones from the last request. That matters more than
 * it sounds: a parent checking on their child over a flaky tailnet should get a
 * slightly stale page, never a broken one.
 *
 * `EventSource` is a browser built-in, so this needs no library and no build
 * step (D-32). The daemon is the only source of these numbers; nothing here
 * computes a remaining time of its own, because two implementations of the same
 * countdown is how they come to disagree.
 */
(function () {
  "use strict";

  var RING_CIRCUMFERENCE = 251.33; // 2 * pi * r, with r = 40 in the SVG
  var RECONNECT_MS = 4000;
  var source = null;

  function cardFor(childId) {
    return document.querySelector('.child-card[data-child="' + childId + '"]');
  }

  function text(card, role, value) {
    var node = card.querySelector('[data-role="' + role + '"]');
    if (node && value !== null && value !== undefined) {
      node.textContent = value;
    }
  }

  function updateRing(card, remaining, quota) {
    var ring = card.querySelector('[data-role="ring"]');
    if (!ring || !quota) return;
    var fraction = Math.max(0, Math.min(1, remaining / quota));
    var value = ring.querySelector(".ring__value");
    if (value) {
      value.setAttribute("stroke-dashoffset", (RING_CIRCUMFERENCE * (1 - fraction)).toFixed(2));
    }
    ring.classList.remove("ring--ok", "ring--warn", "ring--danger");
    ring.classList.add(fraction <= 0.1 ? "ring--danger" : fraction <= 0.25 ? "ring--warn" : "ring--ok");
  }

  function onTick(event) {
    var data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    var card = cardFor(data.child_id);
    if (!card) return;

    /* No label means the server owns this line: an unmanaged child, or one
     * whose state the daemon could not report. Writing a countdown over
     * "no rules are set for this user" would invent supervision that is not
     * happening, which is worse than a number going stale. */
    var label = card.getAttribute("data-label-remaining");
    if (label && typeof data.remaining_minutes === "number") {
      var timeUp = card.getAttribute("data-label-time-up");
      text(
        card,
        "remaining",
        data.remaining_minutes <= 0 && timeUp
          ? timeUp
          : label.replace("{minutes}", data.remaining_minutes)
      );
    }
    if (typeof data.remaining_seconds === "number") {
      updateRing(card, data.remaining_seconds, Number(card.getAttribute("data-quota")) || 0);
    }
    if (data.app) {
      text(card, "activity", data.app);
    }
    var status = card.querySelector('[data-role="status"]');
    if (status && typeof data.online === "boolean") {
      status.classList.toggle("status--online", data.online);
    }
  }

  function reloadCards() {
    /* For anything structural — a lock screen appearing, a child logging in —
     * ask the server for the rendered cards rather than rebuilding them here.
     * The template is the single description of what a card looks like. */
    fetch("/today/cards", { credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.text() : null;
      })
      .then(function (html) {
        var target = document.getElementById("cards");
        if (html && target) target.innerHTML = html;
      })
      .catch(function () {
        /* Offline. The page stays as it was, which is the right outcome. */
      });
  }

  function connect() {
    if (!window.EventSource) return;
    source = new EventSource("/events");

    source.addEventListener("tick", onTick);
    ["end_sequence", "quota_exhausted", "session_changed", "policy_changed",
     "break_started", "break_finished", "timeout_started", "timeout_finished",
     "earned"].forEach(function (kind) {
      source.addEventListener(kind, reloadCards);
    });

    source.addEventListener("daemon_state", function () {
      reloadCards();
    });

    source.onerror = function () {
      source.close();
      window.setTimeout(connect, RECONNECT_MS);
    };
  }

  document.addEventListener("visibilitychange", function () {
    /* A phone that has been in a pocket for an hour shows whatever it last saw.
     * Refresh on the way back rather than trusting a stream that may have been
     * suspended with the tab. */
    if (document.visibilityState === "visible") reloadCards();
  });

  connect();
})();
