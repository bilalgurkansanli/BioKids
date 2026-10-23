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

  /* ------------------------------------------------------- opening (D-208) */

  /* A tap on an app opens it from here, with no screen in between: the tile
   * shows it is opening - a ring that breathes and "Açılıyor…" - and the rest
   * of the screen stays as it was. That ends when the app's window takes the
   * focus from the launcher, when the launcher says no or the app ends as it
   * starts (the calm screen, as before), or after OPENING_MS, whichever comes
   * first. A second tap on a tile that is opening does nothing. The links stay
   * links: without this script they lead to the launching screen, which works
   * without it too. */
  var OPENING_MS = 25000;
  /* The launcher says yes as soon as the app is running, a moment before an
   * app that cannot start has failed. It watches the app for the rest of
   * SCOPE_REFUSAL_TIMEOUT_S (1.5 s) and forgets one that ended with an error;
   * this long after the yes, /running says whether a tracked app is still
   * there. */
  var STILL_THERE_MS = 2000;
  /* An app tile still opening this long after its tap may have opened its
   * window behind the launcher. A tap on it then asks again, which brings an
   * open window to the front and never starts a second copy (the launcher's
   * `_open_again`, D-208); it used to do nothing for the whole 25 seconds:
   * "I tap and nothing opens" (D-227). Not for a site, which opens a window
   * at every tap. */
  var RETAP_MS = 6000;
  var opening = Object.create(null); /* slug -> what is shown for it, and its timer */
  var tileGrid = document.querySelector(".tiles");

  /* The app tiles whose app is running (D-201), or null when that is not known. */
  var askRunning = function () {
    return fetch("/running", { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        return data && Array.isArray(data.running) ? data.running.map(String) : null;
      });
  };

  var launchTarget = function (node) {
    var link = node && node.closest ? node.closest('a[href^="/launch/"]') : null;
    if (!link || !tileGrid) return null;
    var slug = link.getAttribute("data-slug");
    if (!slug) {
      try {
        slug = decodeURIComponent(link.getAttribute("href").slice("/launch/".length));
      } catch (err) {
        return null;
      }
    }
    return slug ? { link: link, slug: slug } : null;
  };

  /* The tile itself, and whatever else was tapped for it: an "Açık" button at
   * the bottom, or the day's card. */
  var openingMarks = function (slug, link) {
    var marks = [];
    var tiles = document.querySelectorAll(".tile[data-slug]");
    for (var i = 0; i < tiles.length; i += 1) {
      if (tiles[i].getAttribute("data-slug") === slug) marks.push(tiles[i]);
    }
    if (link && marks.indexOf(link) === -1) marks.push(link);
    return marks;
  };

  var endOpening = function (slug, entry) {
    if (opening[slug] !== entry) return;
    delete opening[slug];
    window.clearTimeout(entry.timer);
    entry.marks.forEach(function (mark) {
      mark.classList.remove("is-opening");
      mark.removeAttribute("aria-busy");
      var label = mark.querySelector(".tile__opening");
      if (label) label.parentNode.removeChild(label);
    });
  };

  var showOpening = function (slug, link) {
    var entry = {
      marks: openingMarks(slug, link),
      timer: null,
      superseded: false,
      since: Date.now(),
    };
    entry.marks.forEach(function (mark) {
      mark.classList.add("is-opening");
      mark.setAttribute("aria-busy", "true");
      if (mark.classList.contains("tile") && !mark.querySelector(".tile__opening")) {
        var label = document.createElement("span");
        label.className = "tile__opening";
        label.setAttribute("role", "status");
        label.textContent = (window.BIOKIDS_STRINGS && window.BIOKIDS_STRINGS.opening) || "…";
        mark.appendChild(label);
      }
    });
    opening[slug] = entry;
    entry.timer = window.setTimeout(function () {
      endOpening(slug, entry);
    }, OPENING_MS);
    return entry;
  };

  /* An app's window took the focus: whatever was opening has opened. A "no"
   * that arrives after that is about a tap the child has left behind. */
  var openingDone = function () {
    Object.keys(opening).forEach(function (slug) {
      var entry = opening[slug];
      entry.superseded = true;
      endOpening(slug, entry);
    });
  };

  /* The calm screen, for a tap whose ring is still on its tile: not once the
   * 25 seconds have run out or an app's window has taken the focus, and not
   * over the ring of a later tap. The child has left those taps behind, and a
   * screen about one of them would be about nothing they can see. */
  var refused = function (slug, entry, reason) {
    var live = opening[slug] === entry && !entry.superseded;
    endOpening(slug, entry);
    if (!live) return;
    window.location.href = reason === null
      ? "/blocked"
      : "/blocked?reason=" + encodeURIComponent(reason || "") +
        "&slug=" + encodeURIComponent(slug);
  };

  /* A tracked app no longer running this soon after the yes ended as it
   * started: nothing is going to open, and the child is told so - the calm
   * screen, as when the launcher said no - rather than left watching a ring
   * breathe over nothing for 25 seconds, and again at every tap. Nothing when
   * /running does not answer: the ring stays until its usual end. */
  var checkStillThere = function (slug, entry) {
    window.setTimeout(function () {
      if (opening[slug] !== entry) return;
      askRunning()
        .then(function (running) {
          if (running && running.indexOf(slug) === -1) {
            refused(slug, entry, "error.unknown");
          }
        })
        .catch(function () {});
    }, STILL_THERE_MS);
  };

  var lastApp = null; /* the app tile last opened or brought back (D-227) */

  var openApp = function (slug, link) {
    var entry = showOpening(slug, link);
    fetch("/launch/" + encodeURIComponent(slug), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (result) {
        if (!result || !result.ok) {
          refused(slug, entry, (result && result.reason) || "");
        } else if (result.builtin === "shutdown") {
          window.location.href = "/shutdown";
        } else if (result.page) {
          /* A tile that is a page of the launcher's own (D-212). */
          endOpening(slug, entry);
          window.location.href = result.page;
        } else if (result.builtin) {
          endOpening(slug, entry);
        } else if (result.tracked) {
          lastApp = slug;
          checkStillThere(slug, entry);
        }
        /* Otherwise the app is starting, or was brought back: the ring stays
         * until its window is in front - or, for an app tile's app, until
         * /running says it is gone. */
      })
      .catch(function () {
        refused(slug, entry, null);
      });
  };

  if (tileGrid) {
    document.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      var target = launchTarget(event.target);
      if (!target) return;
      event.preventDefault();
      var busy = opening[target.slug];
      if (busy) {
        var kind = target.link.getAttribute("data-kind");
        if ((kind && kind !== "app") || Date.now() - busy.since < RETAP_MS) return;
        busy.superseded = true;
        endOpening(target.slug, busy);
      }
      openApp(target.slug, target.link);
    });
    window.addEventListener("blur", function (event) {
      if (event.target === window) openingDone();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") openingDone();
    });
    /* A page brought back from the browser's cache is not opening anything. */
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) openingDone();
    });
  }

  /* ------------------------------------------------- an app in front (D-227) */

  /* An app whose window cannot fill the screen - Frozen Bubble, TuxMath - floats
   * over the launcher, and the tiles around it could still be tapped. A tap
   * there brought the launcher up over the app, which seemed to vanish, or
   * started another one: "I tap and nothing opens". So while an app's window is
   * in front the launcher is covered. A tap on the cover brings that app back;
   * the cover's 🏠 is the way home. When the launcher comes back without a tap
   * on it - the app was closed, the app bar's 🏠, a time warning - the cover
   * goes by itself. */
  var COVER_TAP_MS = 300;
  var cover = document.getElementById("app-cover");
  if (cover && tileGrid) {
    var coverHome = document.getElementById("app-cover-home");
    var coverSlug = null;
    var pressedAt = 0;
    var hideCover = function () {
      cover.hidden = true;
      coverSlug = null;
    };
    document.addEventListener("pointerdown", function () {
      pressedAt = Date.now();
    }, true);
    window.addEventListener("blur", function (event) {
      if (event.target !== window || !lastApp) return;
      coverSlug = lastApp;
      cover.hidden = false;
    });
    window.addEventListener("focus", function () {
      var focusedAt = Date.now();
      window.setTimeout(function () {
        /* The window manager gives the focus first and the tap follows. */
        if (pressedAt < focusedAt - COVER_TAP_MS) hideCover();
      }, COVER_TAP_MS);
    });
    cover.addEventListener("click", function (event) {
      event.preventDefault();
      var slug = coverSlug;
      hideCover();
      if (coverHome && coverHome.contains(event.target)) return;
      if (slug && !opening[slug]) openApp(slug, null);
    });
  }

  /* ------------------------------------------------------------ launching */

  var launchSlug = document.getElementById("launch-slug");
  var launched = false;
  if (launchSlug) {
    /* "… açılıyor" is true only where this script posts for the child; without
     * it the screen is the icon, the name and an "Aç" button, which a child
     * who cannot read yet understands without a sentence above it. */
    var launchStatus = document.getElementById("launch-status");
    if (launchStatus) launchStatus.hidden = false;
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
        if (result && result.ok && result.page) {
          launched = true;
          window.location.replace(result.page);
        } else if (result && result.ok) {
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

  /* ------------------------------------------------------------- goodbye */

  /* "Bilgisayar kapanıyor…" (D-209). A machine that goes down never runs
   * this; one that stayed up sends the child back to whatever is current. */
  var goodbye = document.getElementById("goodbye");
  if (goodbye) {
    var backAfter = parseInt(goodbye.getAttribute("data-back-after") || "60", 10);
    window.setTimeout(function () {
      window.location.replace("/");
    }, Math.max(10, backAfter) * 1000);
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
  /* The child's birthday (D-236): Zio says it once, the day it is. */
  var birthday = document.querySelector("[data-birthday-say]");
  if (birthday) {
    window.setTimeout(function () {
      sayAloud(birthday.getAttribute("data-birthday-say"));
    }, 900);
  }

  /* Şimdi ne yapsak? (D-231): Zio says what to do next, for every age, a
   * moment after the line the daemon asked for - the voice queues them. */
  var nextThing = document.querySelector("[data-offscreen-say]");
  if (nextThing) {
    /* Once per card: the end screens reload as the countdown and the lock
     * arrive, and the child should hear the suggestion once, not each time. */
    var thing = nextThing.getAttribute("data-offscreen-say");
    var heard = null;
    try {
      heard = window.sessionStorage.getItem("biokids-offscreen-said");
    } catch (err) {
      heard = null;
    }
    if (heard !== thing) {
      try {
        window.sessionStorage.setItem("biokids-offscreen-said", thing);
      } catch (err) {
        /* Without storage it may be said twice; never not at all. */
      }
      window.setTimeout(function () {
        sayAloud(thing);
      }, 1500);
    }
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

  /* The evening's last ten minutes (D-214): the same pause, in the night's
   * colours, with the night's sentence and Zio sleepy. The daemon says which;
   * nothing here decides what time of day it is. */
  function nightFaces(on) {
    if (!notice) return;
    var faces = notice.querySelectorAll(".notice__face svg");
    for (var i = 0; i < faces.length; i += 1) {
      faces[i].classList.toggle("is-sleepy", on);
    }
  }

  function showNotice(minutes, remainingSeconds, bedtime) {
    if (!notice || !noticeLine) return false;
    var strings = window.BIOKIDS_STRINGS || {};
    var lines = window.BIOKIDS_LINES || {};
    var line = bedtime
      ? lines["bedtime_" + minutes] || strings.bedtime_warning
      : lines["warn_" + minutes];
    var fallback = strings.warning ? strings.warning : "{minutes}";
    noticeLine.textContent = (line || fallback).replace("{minutes}", minutes);
    notice.setAttribute("data-level", minutes <= 1 ? "danger" : minutes <= 5 ? "warn" : "info");
    notice.setAttribute("data-tone", bedtime ? "night" : "day");
    notice.setAttribute("data-persona", "zio");
    nightFaces(!!bedtime);
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

  /* The last round (D-230). On the one-minute warning the page asks /state
   * whether one is due - the parent's minutes, once a day, while the quota is
   * what ends the day - and shows "Bitireyim" if so. A tap asks the daemon,
   * which decides again; the answer replaces the sentence, and the screen
   * stays a moment longer so the child sees that it worked. */
  var finishButton = document.getElementById("notice-finish");
  var finishMore = document.getElementById("notice-finish-more");

  function offerFinishUp() {
    if (!finishButton) return;
    fetch("/state", { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        var offer = data && data.finish_up ? Number(data.finish_up.offer_seconds) || 0 : 0;
        if (offer <= 0 || !notice || notice.hidden) return;
        var strings = window.BIOKIDS_STRINGS || {};
        var minutes = Math.round(offer / 60);
        if (finishMore) {
          finishMore.textContent = (strings.finishup_more || "+{minutes}").replace(
            "{minutes}",
            minutes,
          );
        }
        finishButton.disabled = false;
        finishButton.hidden = false;
      })
      .catch(function () {});
  }

  if (finishButton) {
    finishButton.addEventListener("click", function () {
      finishButton.disabled = true;
      fetch("/finish-up", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (result) {
          finishButton.hidden = true;
          if (!result || !result.ok) return;
          var strings = window.BIOKIDS_STRINGS || {};
          if (noticeLine) {
            noticeLine.textContent = (strings.finishup_granted || "+{minutes}").replace(
              "{minutes}",
              result.minutes,
            );
          }
          if (notice) notice.setAttribute("data-level", "info");
          window.clearTimeout(noticeTimer);
          noticeTimer = window.setTimeout(hideNotice, 2500);
        })
        .catch(function () {
          finishButton.hidden = true;
        });
    });
  }

  function hideNotice() {
    if (!notice) return;
    if (finishButton) finishButton.hidden = true;
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

  /* ------------------------------------------------- a parent's notes */

  /* D-233: a "well done" or one sentence from a parent, shown over whatever is
   * on the screen. Live from the event stream, or waiting in the home page from
   * while the child was away; one at a time, each marked seen as it shows. Zio
   * says a waiting "well done" and every note; a live "well done" is already
   * being said by the daemon's own line. */
  var noteCard = document.getElementById("note-card");
  var noteQueue = [];
  var noteTimer = null;

  function noteText(withName, nameless, from) {
    var strings = window.BIOKIDS_STRINGS || {};
    return from ? (strings[withName] || "").replace("{from}", from) : strings[nameless] || "";
  }

  function showNextNote() {
    if (!noteCard || !noteCard.hidden || !noteQueue.length) return;
    var entry = noteQueue.shift();
    var note = entry.note;
    var praise = note.kind === "praise";
    var title = praise
      ? noteText("note_praise", "note_praise_nameless", note.from)
      : noteText("note_from", "note_from_nameless", note.from);
    noteCard.setAttribute("data-kind", praise ? "praise" : "note");
    document.getElementById("note-card-emoji").textContent = praise ? "⭐" : "💌";
    document.getElementById("note-card-title").textContent = title;
    var body = document.getElementById("note-card-text");
    body.textContent = praise ? "" : note.text || "";
    body.hidden = praise;
    noteCard.hidden = false;
    if (!praise || !entry.live) sayAloud(praise ? title : title + " " + (note.text || ""));
    if (note.id) {
      fetch("/notes/" + encodeURIComponent(note.id) + "/seen", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).catch(function () {});
    }
    window.clearTimeout(noteTimer);
    noteTimer = window.setTimeout(hideNote, 15000);
  }

  function hideNote() {
    if (!noteCard) return;
    noteCard.hidden = true;
    window.clearTimeout(noteTimer);
    window.setTimeout(showNextNote, 600);
  }

  function queueNote(note, live) {
    if (!note) return;
    for (var i = 0; i < noteQueue.length; i += 1) {
      if (noteQueue[i].note.id === note.id) return;
    }
    noteQueue.push({ note: note, live: live });
    showNextNote();
  }

  if (noteCard) {
    document.getElementById("note-card-ok").addEventListener("click", hideNote);
    var waiting = document.getElementById("waiting-notes");
    if (waiting) {
      try {
        JSON.parse(waiting.textContent).forEach(function (note) {
          queueNote(note, false);
        });
      } catch (err) {
        /* A note that cannot be read is one fewer note, never a broken page. */
      }
    }
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
      var shown = showNotice(minutes, data.remaining_seconds || 0, !!data.bedtime);
      if (shown && minutes <= 1) offerFinishUp();
      if (!shown) {
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
      var warning = name.indexOf("warn_") === 0 || name.indexOf("bedtime_") === 0;
      if (warning && notice && !notice.hidden) {
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

    /* The app in front was stopped: its kind of time is used up (D-228). The
     * calm screen says so, as it does for a tap on such an app. */
    source.addEventListener("blocked", function (event) {
      var data = parse(event) || {};
      /* Zio is already saying why (D-229): the page stays quiet. */
      window.location.href =
        "/blocked?reason=" + encodeURIComponent(data.reason_code || "") +
        "&slug=" + encodeURIComponent(data.app || "") + "&said=1";
    });

    /* A parent's "well done" or note (D-233), live. */
    source.addEventListener("parent_note", function (event) {
      var data = parse(event);
      queueNote(data && data.note, true);
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

  /* The machine's sound (D-199, D-201): 🔉 and 🔊 step by ten, and the bar
   * between them follows a finger dragged or tapped anywhere along it, or the
   * arrow keys. A drag is dozens of positions a second: the sound is set at
   * most every SEND_MS while it moves, and once more where the finger lifts. */
  var vol = document.getElementById("vol");
  if (vol) {
    var SEND_MS = 120;
    var fill = document.getElementById("vol-fill");
    var knob = document.getElementById("vol-knob");
    var bar = document.getElementById("vol-bar");
    var track = document.getElementById("vol-track");
    var sentLevel = Number(vol.getAttribute("data-level")) || 0;
    var queuedLevel = null;
    var sendTimer = null;

    var sendNow = function () {
      window.clearTimeout(sendTimer);
      sendTimer = null;
      if (queuedLevel === null || queuedLevel === sentLevel) return;
      sentLevel = queuedLevel;
      queuedLevel = null;
      fetch("/device/volume?value=" + sentLevel, { method: "POST" }).catch(function () {});
    };

    var setLevel = function (level, now) {
      level = Math.max(0, Math.min(100, Math.round(level)));
      vol.setAttribute("data-level", String(level));
      if (fill) fill.style.width = level + "%";
      if (knob) knob.style.left = level + "%";
      if (bar) bar.setAttribute("aria-valuenow", String(level));
      queuedLevel = level;
      if (now) {
        sendNow();
      } else if (sendTimer === null) {
        sendTimer = window.setTimeout(sendNow, SEND_MS);
      }
    };

    var current = function () {
      return Number(vol.getAttribute("data-level")) || 0;
    };

    vol.addEventListener("click", function (event) {
      var button = event.target.closest("[data-step]");
      if (!button) return;
      setLevel(current() + Number(button.getAttribute("data-step")), true);
    });

    if (bar && track) {
      var dragging = null;
      var levelAt = function (clientX) {
        var box = track.getBoundingClientRect();
        return box.width > 0 ? ((clientX - box.left) / box.width) * 100 : current();
      };
      bar.addEventListener("pointerdown", function (event) {
        if (event.button !== undefined && event.button > 0) return;
        event.preventDefault();
        dragging = event.pointerId;
        bar.classList.add("is-dragging");
        try {
          bar.setPointerCapture(event.pointerId);
        } catch (err) {
          /* a pointer that is already gone */
        }
        setLevel(levelAt(event.clientX), false);
      });
      bar.addEventListener("pointermove", function (event) {
        if (dragging !== event.pointerId) return;
        setLevel(levelAt(event.clientX), false);
      });
      var release = function (event) {
        if (dragging !== event.pointerId) return;
        dragging = null;
        bar.classList.remove("is-dragging");
        if (event.type === "pointerup") {
          setLevel(levelAt(event.clientX), true);
        } else {
          sendNow();
        }
      };
      bar.addEventListener("pointerup", release);
      bar.addEventListener("pointercancel", release);
      bar.addEventListener("keydown", function (event) {
        var steps = { ArrowLeft: -10, ArrowDown: -10, ArrowRight: 10, ArrowUp: 10 };
        if (event.key === "Home") {
          setLevel(0, true);
        } else if (event.key === "End") {
          setLevel(100, true);
        } else if (steps[event.key] !== undefined) {
          setLevel(current() + steps[event.key], true);
        } else {
          return;
        }
        event.preventDefault();
      });
    }
  }

  /* ------------------------------------------------------------ open apps */

  /* The apps sent to the background with the app bar's 🏠 (D-201), at the
   * bottom as "Açık", with a dot on their tiles. A tap goes through
   * /launch/<slug> like a tap on the tile, which brings the same window back.
   * Asked for on load, every few seconds, and whenever the launcher comes back
   * in front - which is what 🏠 does. */
  var openApps = document.getElementById("open-apps");
  var openList = document.getElementById("open-apps-list");
  if (openApps && openList) {
    var OPEN_POLL_MS = 5000;
    var shownOpen = null;

    var tileFor = function (slug) {
      var tiles = document.querySelectorAll(".tile[data-slug]");
      for (var i = 0; i < tiles.length; i += 1) {
        if (tiles[i].getAttribute("data-slug") === slug) return tiles[i];
      }
      return null;
    };

    var paintOpen = function (slugs) {
      var key = slugs.join(" ");
      if (key === shownOpen) return;
      shownOpen = key;
      Array.prototype.forEach.call(document.querySelectorAll(".tile[data-slug]"), function (tile) {
        tile.classList.toggle("is-open", slugs.indexOf(tile.getAttribute("data-slug")) !== -1);
      });
      openList.textContent = "";
      slugs.forEach(function (slug) {
        var tile = tileFor(slug);
        if (!tile) return;
        var icon = tile.querySelector(".tile__icon");
        var label = tile.querySelector(".tile__label");
        var name = label ? label.textContent.trim() : slug;
        var link = document.createElement("a");
        link.className = "open-app";
        link.href = "/launch/" + encodeURIComponent(slug);
        link.setAttribute("data-slug", slug);
        link.setAttribute("aria-label", name);
        link.title = name;
        if (tile.hasAttribute("data-speak")) {
          link.setAttribute("data-speak", tile.getAttribute("data-speak"));
        }
        var glyph = document.createElement("span");
        glyph.className = "open-app__icon";
        glyph.setAttribute("aria-hidden", "true");
        glyph.textContent = icon ? icon.textContent.trim() : "";
        var dot = document.createElement("span");
        dot.className = "open-app__dot";
        dot.setAttribute("aria-hidden", "true");
        link.appendChild(glyph);
        link.appendChild(dot);
        openList.appendChild(link);
      });
      openApps.hidden = openList.childElementCount === 0;
    };

    var refreshOpen = function () {
      askRunning()
        .then(function (running) {
          if (running) paintOpen(running);
        })
        .catch(function () {});
    };

    refreshOpen();
    window.setInterval(refreshOpen, OPEN_POLL_MS);
    window.addEventListener("focus", refreshOpen);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") refreshOpen();
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
