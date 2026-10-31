/* The BioKids website's only script. Every page works without it: it adds
 * copy buttons, OS tabs, the header's shadow, the table of contents' marker
 * and a hint for a visitor whose browser speaks the other language.
 * It stores nothing but one "dismissed" flag for this tab, and sends nothing. */
(function () {
  "use strict";

  /* The header gets a line once the page has scrolled. */
  var header = document.querySelector(".site-header");
  if (header) {
    var onScroll = function () { header.classList.toggle("scrolled", window.scrollY > 8); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* The phone menu closes when a link in it is followed or Escape is pressed. */
  var menu = document.querySelector(".menu");
  if (menu) {
    menu.addEventListener("click", function (e) {
      if (e.target.closest("a")) menu.removeAttribute("open");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.hasAttribute("open")) {
        menu.removeAttribute("open");
        menu.querySelector("summary").focus();
      }
    });
    document.addEventListener("click", function (e) {
      if (menu.hasAttribute("open") && !menu.contains(e.target)) menu.removeAttribute("open");
    });
  }

  /* Copy buttons on commands. */
  document.querySelectorAll(".code-copy").forEach(function (btn) {
    var label = btn.textContent;
    btn.addEventListener("click", function () {
      var code = btn.parentElement.querySelector("code");
      var text = code ? code.innerText.replace(/\n$/, "") : "";
      var done = function () {
        btn.textContent = btn.getAttribute("data-copied") || "✓";
        btn.classList.add("done");
        setTimeout(function () { btn.textContent = label; btn.classList.remove("done"); }, 1800);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (err) { /* nothing to do */ }
        document.body.removeChild(ta);
      }
    });
  });

  /* Tabs: a .tabs holding .tab-panel[data-tab] each with an .tab-title.
   * Without the script every panel shows, one under the other. The tab that
   * matches the visitor's system opens first. */
  var guessOS = function () {
    var ua = navigator.userAgent || "";
    if (/Windows/i.test(ua)) return "windows";
    if (/Mac OS X|Macintosh/i.test(ua) && !/iPhone|iPad/i.test(ua)) return "macos";
    if (/Linux|X11/i.test(ua) && !/Android/i.test(ua)) return "linux";
    return "";
  };
  document.querySelectorAll(".tabs").forEach(function (tabs, ti) {
    var panels = Array.prototype.slice.call(tabs.querySelectorAll(":scope > .tab-panel"));
    if (panels.length < 2) return;
    var nav = document.createElement("div");
    nav.className = "tabs-nav";
    nav.setAttribute("role", "tablist");
    var buttons = panels.map(function (panel, i) {
      var id = "tab-" + ti + "-" + i;
      var b = document.createElement("button");
      b.type = "button";
      b.id = id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", panel.id || (panel.id = id + "-panel"));
      b.textContent = panel.getAttribute("data-label") || (panel.querySelector(".tab-title") || {}).textContent || String(i + 1);
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", id);
      nav.appendChild(b);
      return b;
    });
    var select = function (i, focus) {
      buttons.forEach(function (b, j) {
        b.setAttribute("aria-selected", String(i === j));
        b.tabIndex = i === j ? 0 : -1;
        panels[j].hidden = i !== j;
      });
      if (focus) buttons[i].focus();
    };
    buttons.forEach(function (b, i) {
      b.addEventListener("click", function () { select(i); });
      b.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight") select((i + 1) % buttons.length, true);
        if (e.key === "ArrowLeft") select((i - 1 + buttons.length) % buttons.length, true);
      });
    });
    tabs.insertBefore(nav, panels[0]);
    tabs.classList.add("js");
    var os = guessOS();
    var start = 0;
    panels.forEach(function (p, i) { if (os && p.getAttribute("data-tab") === os) start = i; });
    select(start);
  });

  /* The table of contents marks the part of the page being read. */
  var tocLinks = document.querySelectorAll(".toc a");
  if (tocLinks.length && "IntersectionObserver" in window) {
    var byId = {};
    tocLinks.forEach(function (a) { byId[decodeURIComponent(a.hash.slice(1))] = a; });
    var visible = {};
    var mark = function () {
      var first = null;
      Object.keys(byId).forEach(function (id) {
        var el = document.getElementById(id);
        if (visible[id] && el && (!first || el.getBoundingClientRect().top < first.getBoundingClientRect().top)) first = el;
      });
      if (!first) return;
      tocLinks.forEach(function (a) { a.classList.toggle("active", a === byId[first.id]); });
    };
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      mark();
    }, { rootMargin: "-80px 0px -55% 0px" });
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) io.observe(el);
    });
  }

  /* A visitor whose browser does not speak this page's language is told,
   * once per tab, that the other one exists. Never redirected. */
  var hint = document.querySelector("[data-lang-hint]");
  if (hint) {
    var pageLang = hint.getAttribute("data-lang-hint");
    var langs = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""])
      .map(function (l) { return String(l).toLowerCase().slice(0, 2); });
    var speaksPage = langs.indexOf(pageLang) !== -1;
    var other = pageLang === "tr" ? "en" : "tr";
    var dismissed = false;
    try { dismissed = sessionStorage.getItem("bk-lang-hint") === "1"; } catch (err) { /* private mode */ }
    if (!speaksPage && langs.indexOf(other) !== -1 && !dismissed) {
      hint.hidden = false;
      hint.querySelector(".lang-hint-close").addEventListener("click", function () {
        hint.hidden = true;
        try { sessionStorage.setItem("bk-lang-hint", "1"); } catch (err) { /* private mode */ }
      });
    }
  }
})();
