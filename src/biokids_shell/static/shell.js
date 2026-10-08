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
  var launched = false;
  if (launchSlug) {
    /* Whenever the launcher is back in front - the child closed the app with
     * its big X - and the loading screen is still showing, it is stale. */
    var backHome = function () {
      if (document.visibilityState === "visible" && launched) {
        window.location.replace("/");
      }
    };
    window.addEventListener("focus", backHome);
    document.addEventListener("visibilitychange", backHome);
    window.setTimeout(function () {
      launched = true;
    }, 4000);
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
           * child is never left looking at a loading screen that has finished.
           * Straight away: the app's window covers this one at once, and
           * Chromium may not run a covered page's timers - so a 1.2-second timer
           * left "Boya açılıyor" on screen until the child closed Boya. */
          launched = true;
          window.location.replace("/");
        } else {
          window.location.href =
            "/blocked?reason=" + encodeURIComponent((result && result.reason) || "") +
            "&slug=" + encodeURIComponent(slug);
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
  var ringNumber = document.getElementById("ring-number");

  function minutesLeft(remainingSeconds) {
    return Math.ceil(Math.max(0, remainingSeconds) / 60);
  }

  function updateRing(remainingSeconds) {
    if (!ringWrap || !ringValue) return;
    var quota = parseInt(ringWrap.getAttribute("data-quota") || "0", 10);
    var fraction = quota > 0 ? Math.max(0, Math.min(1, remainingSeconds / quota)) : 0;
    ringValue.setAttribute("stroke-dashoffset", (RING_CIRCUMFERENCE * (1 - fraction)).toFixed(2));
    ringWrap.setAttribute(
      "data-tone",
      fraction <= 0.1 ? "danger" : fraction <= 0.25 ? "warn" : "ok",
    );
    var sun = document.getElementById("ring-sun");
    if (sun) {
      var angle = 2 * Math.PI * fraction;
      sun.setAttribute("cx", (50 + 40 * Math.cos(angle)).toFixed(2));
      sun.setAttribute("cy", (50 + 40 * Math.sin(angle)).toFixed(2));
    }
    var minutes = minutesLeft(remainingSeconds);
    if (ringNumber) {
      ringNumber.textContent = String(minutes);
    }
    if (ringLabel && window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.remaining) {
      ringLabel.setAttribute(
        "aria-label",
        window.BIOKIDS_STRINGS.remaining.replace("{minutes}", String(minutes)),
      );
    }
  }

  /* ------------------------------------------------- read aloud (D-175) */

  /* A four-year-old cannot read a tile's name. In the young band a tap says it,
   * and a screen with a sentence on it says that once as it opens. Throttled:
   * a child drumming on the tiles hears names, not a pile-up. */
  var young = !!document.querySelector('[data-band="4-7"]');
  var lastSaid = "";
  var lastSaidAt = 0;

  function sayAloud(text) {
    text = (text || "").replace(/\s+/g, " ").trim();
    var now = Date.now();
    if (!text || (text === lastSaid && now - lastSaidAt < 3000) || now - lastSaidAt < 700) {
      return;
    }
    lastSaid = text;
    lastSaidAt = now;
    fetch("/speak-label", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    }).catch(function () {});
  }

  if (young) {
    document.addEventListener("pointerdown", function (event) {
      var target = event.target && event.target.closest && event.target.closest("[data-speak]");
      if (target) sayAloud(target.getAttribute("data-speak"));
    });
  }
  var spoken = document.querySelector("[data-speak-on-load]");
  if (spoken) {
    var parts = [];
    spoken.querySelectorAll("h1, p").forEach(function (node) {
      parts.push(node.textContent);
    });
    window.setTimeout(function () {
      sayAloud(parts.join(". "));
    }, 400);
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

  /* ---------------------------------------------------------- time warning */

  /* D-160. The relay brings this window in front of the child's app for
   * NOTICE_MS, so the warning is a full screen of its own: the character, the
   * sentence, the ring. The same number decides both, server and page. */
  var NOTICE_MS = window.BIOKIDS_NOTICE_MS || 7000;
  var notice = document.getElementById("notice");
  var noticeLine = document.getElementById("notice-line");
  var noticeRing = document.getElementById("notice-ring");
  var noticeRingNumber = document.getElementById("notice-ring-number");
  var noticeTimer = null;

  function speakingFaces(on) {
    if (!notice) return;
    var faces = notice.querySelectorAll(".notice__face svg");
    for (var i = 0; i < faces.length; i += 1) {
      faces[i].classList.toggle("is-speaking", on);
    }
  }

  function showNotice(minutes, remainingSeconds) {
    if (!notice || !noticeLine) return false;
    var line = window.BIOKIDS_LINES && window.BIOKIDS_LINES["warn_" + minutes];
    var fallback =
      window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.warning
        ? window.BIOKIDS_STRINGS.warning
        : "{minutes}";
    noticeLine.textContent = (line || fallback).replace("{minutes}", minutes);
    notice.setAttribute("data-level", minutes <= 1 ? "danger" : minutes <= 5 ? "warn" : "info");
    notice.setAttribute("data-persona", "zio");
    if (noticeRing) {
      var wrap = document.getElementById("ring-wrap");
      var quota = wrap ? Number(wrap.getAttribute("data-quota")) : 0;
      var fraction = quota > 0 ? Math.max(0, Math.min(1, remainingSeconds / quota)) : 0;
      noticeRing.setAttribute("stroke-dashoffset", String(RING_CIRCUMFERENCE * (1 - fraction)));
    }
    if (noticeRingNumber) {
      noticeRingNumber.textContent = String(
        remainingSeconds > 0 ? minutesLeft(remainingSeconds) : minutes,
      );
    }
    notice.hidden = false;
    window.requestAnimationFrame(function () {
      notice.classList.add("is-visible");
    });
    speakingFaces(true);
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(hideNotice, NOTICE_MS);
    return true;
  }

  function hideNotice() {
    if (!notice) return;
    notice.classList.remove("is-visible");
    speakingFaces(false);
    window.setTimeout(function () {
      notice.hidden = true;
    }, 400);
  }

  /* The relay keeps what the voice service actually chose to say, and in which
   * voice. Asked for a moment after the speak event, so the words on the
   * screen are the words being heard and the character shown is the one
   * speaking (VOICE.md §7, D-160). Unanswered, the catalog sentence stays. */
  function whenSpoken(eventName, apply) {
    var attempts = 0;
    function ask() {
      attempts += 1;
      fetch("/spoken", { cache: "no-store" })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (spoken) {
          var fresh = spoken && spoken.at && Math.abs(Date.now() / 1000 - spoken.at) < 20;
          if (fresh && spoken.event === eventName && spoken.text) {
            apply(spoken);
          } else if (attempts < 3) {
            window.setTimeout(ask, 500);
          }
        })
        .catch(function () {});
    }
    window.setTimeout(ask, 300);
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
      if (!showNotice(minutes, data.remaining_seconds || 0)) {
        showBanner(
          (window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.warning
            ? window.BIOKIDS_STRINGS.warning
            : "{minutes}"
          ).replace("{minutes}", minutes),
          minutes <= 1 ? "danger" : minutes <= 5 ? "warn" : "info",
        );
      }
    });

    source.addEventListener("speak", function (event) {
      var data = parse(event);
      if (!data) return;
      var name = data.event || "";
      if (name.indexOf("warn_") === 0 && notice && !notice.hidden) {
        /* The warning already fills the screen; it takes the spoken words. */
        whenSpoken(name, function (spoken) {
          noticeLine.textContent = spoken.text;
          notice.setAttribute("data-persona", spoken.persona === "sio" ? "sio" : "zio");
        });
        return;
      }
      var line = window.BIOKIDS_LINES && window.BIOKIDS_LINES[name];
      showCard(
        (line || "").replace("{minutes}", data.vars ? data.vars.minutes : ""),
        data.persona === "sio" ? "sio" : "zio",
        data.expression,
      );
      whenSpoken(name, function (spoken) {
        if (cardText) cardText.textContent = spoken.text;
      });
    });

    /* Anything that changes which screen the child should be on is settled by
     * the server, not here: reload and let the router decide. */
    ["end_sequence", "break_started", "break_finished", "timeout_started",
     "timeout_finished", "quota_exhausted", "policy_changed"].forEach(function (kind) {
      source.addEventListener(kind, function () {
        window.location.href = "/";
      });
    });

    /* A parent answered (D-173). The banner always; Zio or Sio says it too
     * when the voices are on, through the ordinary speak event. A yes to more
     * time also arrives as a tick, which moves the ring. */
    source.addEventListener("request_decided", function (event) {
      var data = parse(event);
      if (!data) return;
      var strings = window.BIOKIDS_STRINGS || {};
      var text = !data.approved
        ? strings.request_denied
        : data.request_kind === "time"
          ? (strings.request_approved_time || "").replace("{minutes}", data.minutes || 0)
          : strings.request_approved;
      showBanner(text || "", data.approved ? "info" : "warn");
      var asked = document.getElementById("ask-time");
      if (asked) asked.hidden = true;
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

  var vol = document.getElementById("vol");
  if (vol) {
    var fill = document.getElementById("vol-fill");
    vol.addEventListener("click", function (event) {
      var button = event.target.closest("[data-step]");
      if (!button) return;
      var level = Number(vol.getAttribute("data-level")) || 0;
      level = Math.max(0, Math.min(100, level + Number(button.getAttribute("data-step"))));
      vol.setAttribute("data-level", String(level));
      if (fill) fill.style.width = level + "%";
      fetch("/device/volume?value=" + level, { method: "POST" }).catch(function () {});
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
