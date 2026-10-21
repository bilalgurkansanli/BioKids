/* Çıkartma albümüm - the sticker album (D-211).
 *
 * Two small jobs; the album itself is drawn by the server and needs neither.
 *
 *   1. The day's celebration goes by itself after a while, or at ✕, without
 *      reloading the page.
 *   2. Whenever the launcher comes back in front - the child closed the
 *      learning app they earned the sticker in - ask the launcher whether
 *      there is news: the chip takes the new number, and the first time today
 *      the celebration shows here. Zio says it; the server makes that call,
 *      once a day, so this file never speaks.
 */
(function () {
  "use strict";

  var SHOWN_MS = 12000;
  var ASK_GAP_MS = 2000;

  var cheer = document.getElementById("cheer");
  var chip = document.getElementById("album-chip");
  var timer = null;

  function still() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function hide() {
    if (!cheer || cheer.hidden) return;
    window.clearTimeout(timer);
    if (still()) {
      cheer.hidden = true;
      return;
    }
    cheer.classList.add("is-leaving");
    window.setTimeout(function () {
      cheer.hidden = true;
      cheer.classList.remove("is-leaving");
    }, 260);
  }

  function show(news) {
    if (!cheer) return;
    var sticker = document.getElementById("cheer-sticker");
    var line = document.getElementById("cheer-line");
    if (sticker) sticker.textContent = news.sticker || "";
    if (line) line.textContent = news.text || "";
    cheer.classList.remove("is-leaving");
    cheer.hidden = false;
    window.clearTimeout(timer);
    timer = window.setTimeout(hide, SHOWN_MS);
  }

  if (cheer) {
    var close = document.getElementById("cheer-close");
    if (close) {
      close.addEventListener("click", function (event) {
        event.preventDefault();
        hide();
      });
    }
    if (!cheer.hidden) timer = window.setTimeout(hide, SHOWN_MS);
  }

  /* Only the home page has the chip, and only there is it worth asking. */
  if (!chip) return;
  var count = document.getElementById("album-chip-count");
  var askedAt = 0;

  function ask() {
    var now = Date.now();
    if (now - askedAt < ASK_GAP_MS) return;
    askedAt = now;
    fetch("/album/cheer", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (news) {
        if (!news) return;
        if (typeof news.count === "number" && count) {
          var before = count.textContent;
          count.textContent = String(news.count);
          if (news.label) chip.setAttribute("aria-label", news.label);
          if (before !== count.textContent && !still()) {
            chip.classList.remove("is-bumped");
            void chip.offsetWidth;
            chip.classList.add("is-bumped");
          }
        }
        if (news.cheer) show(news.cheer);
      })
      .catch(function () {});
  }

  window.addEventListener("focus", ask);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") ask();
  });
})();
