/* Bugünün sürprizi (D-203): the button that puts today's card away.
 *
 * The card itself is drawn by the server and reads without this file. Closed,
 * it stays closed for the rest of the day in this child's own browser profile
 * (the date, nothing else), and tomorrow's card is new anyway. A script that
 * cannot store it still closes the card for this visit.
 */
(function () {
  "use strict";

  var KEY = "biokids.daily.hidden";
  var card = document.getElementById("daily");
  var button = document.getElementById("daily-hide");
  if (!card || !button) return;

  button.hidden = false;
  button.addEventListener("click", function () {
    try {
      window.localStorage.setItem(KEY, card.getAttribute("data-day") || "");
    } catch (err) {
      /* No storage: closed until the page is drawn again. */
    }
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still) {
      card.hidden = true;
      return;
    }
    card.classList.add("is-leaving");
    window.setTimeout(function () {
      card.hidden = true;
    }, 220);
  });
})();
