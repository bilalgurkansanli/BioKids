/* The launcher's behaviour.
 *
 * Four jobs, and deliberately no fifth:
 *   1. make a tap feel instant, whatever the app does afterwards
 *   2. keep the ring and the banners in step with the daemon
 *   3. show Zio and Sio when they speak, with the words on screen
 *   4. close off the ways out of the window that a page is allowed to close
 *
 * Nothing here computes a remaining time. The daemon is the only thing that
 * knows how much time a child has, and a second implementation of that sum in
 * a page a child can reach is how the two come to disagree.
 */
(function () {
  "use strict";

  var RING_CIRCUMFERENCE = 251.33;
  var BANNER_MS = 8000;
  var CARD_MS = 6000;

  /* ------------------------------------------------------------- escapes */

  /* A child will try these. None of them is a real barrier — Alt+F4 and
   * Ctrl+Alt+F2 never reach a web page and are the window manager's job
   * (SECURITY.md §1) — but the ones a page *can* take, it takes. */
  document.addEventListener("contextmenu", function (event) {
    event.preventDefault();
  });

  document.addEventListener("dragstart", function (event) {
    event.preventDefault();
  });

  document.addEventListener("keydown", function (event) {
    var key = (event.key || "").toLowerCase();
    var blockedWithCtrl = ["w", "t", "n", "r", "p", "s", "o", "j", "u", "f"];
    if ((event.ctrlKey || event.metaKey) && blockedWithCtrl.indexOf(key) !== -1) {
      event.preventDefault();
    }
    if (key === "f11" || key === "f12" || key === "f5" || key === "escape") {
      event.preventDefault();
    }
    if (event.altKey && (key === "arrowleft" || key === "arrowright")) {
      event.preventDefault();
    }
  });

  /* ---------------------------------------------------------------- clock */

  var clock = document.getElementById("clock");
  if (clock) {
    var tickClock = function () {
      var now = new Date();
      clock.textContent =
        String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0");
    };
    tickClock();
    window.setInterval(tickClock, 15000);
  }

  /* ---------------------------------------------------------------- tiles */

  /* The press is painted before the navigation, so the 100ms feedback rule
   * holds even when the machine is busy starting something. */
  Array.prototype.forEach.call(document.querySelectorAll(".tile"), function (tile) {
    tile.addEventListener("pointerdown", function () {
      tile.classList.add("is-pressed");
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach(function (name) {
      tile.addEventListener(name, function () {
        tile.classList.remove("is-pressed");
      });
    });
  });

  /* ------------------------------------------------------------ launching */

  var launchSlug = document.getElementById("launch-slug");
  if (launchSlug) {
    var slug = launchSlug.getAttribute("data-slug");
    fetch("/launch/" + encodeURIComponent(slug), {
      method: "POST",
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (result) {
        if (result && result.ok) {
          /* The app is starting in its own window. Put the launcher back so the
           * child is never left looking at a loading screen that has finished. */
          window.setTimeout(function () {
            window.location.href = "/";
          }, 1200);
        } else {
          window.location.href = "/blocked?reason=" + encodeURIComponent((result && result.reason) || "");
        }
      })
      .catch(function () {
        window.location.href = "/blocked";
      });
  }

  /* ------------------------------------------------------------ countdown */

  var countdown = document.getElementById("countdown");
  if (countdown) {
    var left = parseInt(countdown.getAttribute("data-seconds") || "0", 10);
    var paint = function () {
      var minutes = Math.floor(left / 60);
      var seconds = left % 60;
      countdown.textContent =
        minutes > 0
          ? minutes + ":" + String(seconds).padStart(2, "0")
          : String(seconds);
      if (left <= 0) {
        window.location.href = "/";
        return;
      }
      left -= 1;
    };
    paint();
    window.setInterval(paint, 1000);
  }

  /* ----------------------------------------------------------------- ring */

  var ringWrap = document.getElementById("ring-wrap");
  var ringValue = document.getElementById("ring-value");
  var ringLabel = document.getElementById("ring-label");

  function updateRing(remainingSeconds) {
    if (!ringWrap || !ringValue) return;
    var quota = parseInt(ringWrap.getAttribute("data-quota") || "0", 10);
    var fraction = quota > 0 ? Math.max(0, Math.min(1, remainingSeconds / quota)) : 0;
    ringValue.setAttribute("stroke-dashoffset", (RING_CIRCUMFERENCE * (1 - fraction)).toFixed(2));
    ringWrap.setAttribute(
      "data-tone",
      fraction <= 0.1 ? "danger" : fraction <= 0.25 ? "warn" : "ok",
    );
    if (ringLabel) {
      ringLabel.textContent = String(Math.ceil(Math.max(0, remainingSeconds) / 60));
    }
  }

  /* --------------------------------------------------------------- banner */

  var banner = document.getElementById("banner");
  var bannerText = document.getElementById("banner-text");
  var bannerTimer = null;

  function showBanner(text, level) {
    if (!banner || !bannerText || !text) return;
    bannerText.textContent = text;
    banner.setAttribute("data-level", level || "info");
    banner.classList.add("is-visible");
    window.clearTimeout(bannerTimer);
    bannerTimer = window.setTimeout(function () {
      banner.classList.remove("is-visible");
    }, BANNER_MS);
  }

  /* ---------------------------------------------------------- speech card */

  var card = document.getElementById("speech");
  var cardText = document.getElementById("speech-text");
  var cardCharacter = document.getElementById("speech-character");
  var cardTimer = null;

  function showCard(text, persona, expression) {
    if (!card || !cardText) return;
    /* The spoken line always appears as text as well: a deaf child, a muted
     * machine and a noisy room all get the same information (VOICE.md §7). */
    cardText.textContent = text || "";
    card.setAttribute("data-persona", persona || "zio");
    if (cardCharacter) {
      cardCharacter.setAttribute("data-expression", expression || "neutral");
      var svg = cardCharacter.querySelector("svg");
      if (svg) {
        /* The SVG carries all four faces and picks one with an `is-*` class on
         * its root. Toggle, never replace: the base classes are what make the
         * character look like itself. */
        ["happy", "concerned", "sleepy"].forEach(function (name) {
          svg.classList.toggle("is-" + name, name === expression);
        });
      }
    }
    card.classList.add("is-visible");
    window.clearTimeout(cardTimer);
    cardTimer = window.setTimeout(function () {
      card.classList.remove("is-visible");
    }, CARD_MS);
  }

  /* -------------------------------------------------------------- events */

  function connect() {
    if (!window.EventSource) return;
    var source = new EventSource("/events");

    source.addEventListener("tick", function (event) {
      var data = parse(event);
      if (data && typeof data.remaining_seconds === "number") {
        updateRing(data.remaining_seconds);
      }
    });

    source.addEventListener("warning", function (event) {
      var data = parse(event);
      if (!data) return;
      updateRing(data.remaining_seconds || 0);
      var minutes = data.minutes || 0;
      showBanner(
        (window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.warning
          ? window.BIOKIDS_STRINGS.warning
          : "{minutes}"
        ).replace("{minutes}", minutes),
        minutes <= 1 ? "danger" : minutes <= 5 ? "warn" : "info",
      );
    });

    source.addEventListener("speak", function (event) {
      var data = parse(event);
      if (!data) return;
      var line = window.BIOKIDS_LINES && window.BIOKIDS_LINES[data.event];
      showCard(
        (line || "").replace("{minutes}", data.vars ? data.vars.minutes : ""),
        data.persona === "sio" ? "sio" : "zio",
        data.expression,
      );
    });

    /* Anything that changes which screen the child should be on is settled by
     * the server, not here: reload and let the router decide. */
    ["end_sequence", "break_started", "break_finished", "timeout_started",
     "timeout_finished", "quota_exhausted", "policy_changed"].forEach(function (kind) {
      source.addEventListener(kind, function () {
        window.location.href = "/";
      });
    });

    source.addEventListener("earned", function (event) {
      var data = parse(event);
      showBanner(
        (window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.earned
          ? window.BIOKIDS_STRINGS.earned
          : "+{minutes}"
        ).replace("{minutes}", (data && data.minutes) || 0),
        "info",
      );
    });

    var badge = document.getElementById("supervision");
    source.addEventListener("ai_checked", function () {
      if (!badge) return;
      badge.classList.add("is-checking");
      window.setTimeout(function () {
        badge.classList.remove("is-checking");
      }, 2600);
    });

    source.onerror = function () {
      source.close();
      window.setTimeout(connect, 3000);
    };
  }

  function parse(event) {
    try {
      return JSON.parse(event.data);
    } catch (err) {
      return null;
    }
  }

  /* --------------------------------------------------------- volume/light */

  var volume = document.getElementById("volume");
  if (volume) {
    volume.addEventListener("input", function () {
      fetch("/device/volume?value=" + volume.value, { method: "POST" }).catch(function () {});
    });
  }

  var brightness = document.getElementById("brightness");
  if (brightness) {
    brightness.addEventListener("input", function () {
      fetch("/device/brightness?value=" + brightness.value, { method: "POST" }).catch(function () {});
    });
  }

  connect();
})();
