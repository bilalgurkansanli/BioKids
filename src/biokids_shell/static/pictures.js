/* Resimlerim's viewer (D-212): the keys and the swipe.
 *
 * The page already works without this file: ‹, › and ✕ are links. This adds
 * the arrow keys and Escape for a child at a keyboard, and a sideways swipe
 * for one at a touchscreen. The grid is newest first, so → (or a swipe to
 * the left) is the next picture along it, an older one.
 */
(function () {
  "use strict";

  var SWIPE_PX = 60;
  var viewer = document.getElementById("viewer");
  if (!viewer) return;

  function go(id) {
    var link = document.getElementById(id);
    if (link && link.href) window.location.href = link.href;
  }

  /* shell.js already stops Escape reaching the browser (it never leaves the
   * kiosk); here it closes the picture. */
  document.addEventListener("keydown", function (event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    switch (event.key) {
      case "ArrowRight":
        go("viewer-older");
        break;
      case "ArrowLeft":
        go("viewer-newer");
        break;
      case "Escape":
        go("viewer-close");
        break;
      default:
        return;
    }
    event.preventDefault();
  });

  var startX = null;
  var startY = null;
  viewer.addEventListener("pointerdown", function (event) {
    if (event.target.closest && event.target.closest("a")) return;
    startX = event.clientX;
    startY = event.clientY;
  });
  viewer.addEventListener("pointerup", function (event) {
    if (startX === null) return;
    var dx = event.clientX - startX;
    var dy = event.clientY - startY;
    startX = null;
    if (Math.abs(dx) < SWIPE_PX || Math.abs(dx) < Math.abs(dy)) return;
    go(dx < 0 ? "viewer-older" : "viewer-newer");
  });
  viewer.addEventListener("pointercancel", function () {
    startX = null;
  });
})();
