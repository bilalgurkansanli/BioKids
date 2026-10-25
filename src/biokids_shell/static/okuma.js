/* Okuma köşesi (D-241): one lesson, step by step.
 *
 * Five kinds of step, all answered with a tap and nothing typed:
 *   letter   - the letter big, its picture, Zio making its sound;
 *   sound    - Zio says a syllable, the child taps the letter it starts with;
 *   syllable - Zio says a syllable, the child taps it written among three;
 *   word     - a word written, split as the class splits it; the child reads
 *              it and taps its picture. Zio reads it only after the answer
 *              is in - or, after two tries, in syllables, to help;
 *   sentence - the same with a short sentence.
 * The first answer to a step moves its box (POST /okuma/answer); after that
 * the child simply tries again until it is right. A wrong answer is a shake
 * and the sound once more, never a cost.
 */
(function () {
  "use strict";

  var data = {};
  try {
    data = JSON.parse(document.getElementById("okuma-data").textContent);
  } catch (err) {
    return;
  }
  var steps = data.steps || [];
  var strings = data.strings || {};
  var stage = document.getElementById("okuma-stage");
  var bar = document.getElementById("okuma-progress");
  var starsNode = document.getElementById("okuma-stars");
  var index = 0;
  var stars = 0;
  var finished = false;

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    })
      .then(function (response) {
        return response.json();
      })
      .catch(function () {
        return {};
      });
  }

  function say(text) {
    if (!text) return Promise.resolve({});
    return post("/okuma/say", { text: text });
  }

  function praise() {
    var list = data.praise || [];
    return list.length ? list[Math.floor(Math.random() * list.length)] : "";
  }

  function answer(step, right) {
    if (data.practice || step.answered) return;
    step.answered = true;
    post("/okuma/answer", { item: step.item, right: right });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(className, text, onTap) {
    var node = el("button", className, text);
    node.type = "button";
    node.addEventListener("click", onTap);
    return node;
  }

  function upper(text) {
    return String(text).replace(/i/g, "İ").replace(/ı/g, "I").toUpperCase();
  }

  function progress() {
    if (bar) bar.style.width = Math.round((100 * index) / Math.max(1, steps.length)) + "%";
    if (starsNode) starsNode.textContent = String(stars);
  }

  function next() {
    index += 1;
    progress();
    if (index >= steps.length) {
      finish();
    } else {
      show(steps[index]);
    }
  }

  function finish() {
    /* A lesson that is over is not learning, however long its last card stays
     * on the screen: the reports stop (D-232). */
    finished = true;
    stage.innerHTML = "";
    var done = el("div", "okuma__card okuma__card--done");
    done.appendChild(el("p", "okuma__big", "⭐ " + stars));
    done.appendChild(el("p", "okuma__line", data.goodbye || ""));
    var home = el("a", "big-btn big-btn--ok", strings.back || "");
    home.href = "/okuma";
    done.appendChild(home);
    stage.appendChild(done);
    say(data.goodbye);
  }

  function hearButton(text) {
    return button("okuma__hear", "🔊", function () {
      say(text);
    });
  }

  function showLetter(step) {
    var card = el("div", "okuma__card okuma__card--letter");
    var pair = el("p", "okuma__pair");
    pair.appendChild(el("span", "okuma__upper", step.upper || upper(step.show)));
    pair.appendChild(el("span", "okuma__lower", step.show));
    card.appendChild(pair);
    var example = el("p", "okuma__example");
    example.appendChild(el("span", "okuma__pic", step.pic || ""));
    example.appendChild(el("span", "okuma__word", step.syllables || ""));
    card.appendChild(example);
    var row = el("div", "okuma__row");
    row.appendChild(hearButton(step.say));
    row.appendChild(
      button("big-btn big-btn--ok okuma__next", (strings.next || "") + " ▶", function () {
        /* Once: a double tap skipped the next letter's introduction (D-250). */
        if (step.passed) return;
        step.passed = true;
        answer(step, true);
        next();
      }),
    );
    card.appendChild(row);
    stage.appendChild(card);
    say(step.say);
  }

  function showChoice(step) {
    var reading = step.kind === "word" || step.kind === "sentence";
    var card = el("div", "okuma__card okuma__card--" + step.kind);
    var prompt = {
      sound: strings.which_letter,
      syllable: strings.which_syllable,
      word: strings.read_word,
      sentence: strings.read_sentence,
    }[step.kind];
    card.appendChild(el("p", "okuma__prompt", prompt || ""));
    if (reading) {
      card.appendChild(el("p", "okuma__text okuma__text--" + step.kind, step.show));
      if (step.kind === "word" && step.syllables && step.syllables !== step.show) {
        card.appendChild(el("p", "okuma__syllables", step.syllables.split("-").join(" · ")));
      }
    } else {
      card.appendChild(hearButton(step.say));
    }
    var choices = el("div", "okuma__choices");
    var tries = 0;
    (step.choices || []).forEach(function (choice) {
      var label = step.kind === "sound" ? upper(choice) + choice : choice;
      var node = button(
        "okuma__choice" + (reading ? " okuma__choice--pic" : ""),
        label,
        function () {
          var right = choice === step.answer;
          answer(step, right);
          if (right) {
            node.classList.add("is-right");
            stars += 1;
            progress();
            Array.prototype.forEach.call(choices.children, function (other) {
              other.disabled = true;
            });
            var then = reading ? say(step.say).then(function () { return say(praise()); }) : say(praise());
            then.then(function () {
              window.setTimeout(next, 400);
            });
          } else {
            tries += 1;
            node.classList.add("is-wrong");
            window.setTimeout(function () {
              node.classList.remove("is-wrong");
            }, 600);
            if (!reading) {
              say(step.say);
            } else if (tries >= 2) {
              /* Help, in syllables: the reading is still theirs to finish. */
              say((step.syllables || step.show).split("-").join(", "));
            } else {
              say(strings.try_again);
            }
          }
        },
      );
      choices.appendChild(node);
    });
    card.appendChild(choices);
    stage.appendChild(card);
    if (!reading) say(step.say);
  }

  function show(step) {
    stage.innerHTML = "";
    if (step.kind === "letter") {
      showLetter(step);
    } else {
      showChoice(step);
    }
  }

  /* While the page is in front its minutes are learning minutes: the daemon
   * trusts a report for ninety seconds (D-228), so every forty-five. */
  function active() {
    if (!finished && document.visibilityState === "visible") post("/okuma/active", {});
  }
  active();
  window.setInterval(active, 45000);

  progress();
  if (steps.length) {
    show(steps[0]);
  } else {
    finish();
  }
})();
