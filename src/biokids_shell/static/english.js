/* The English corner (D-225).
 *
 * On every page of the corner, a button with `data-say-en` says its word in
 * English when tapped. On the lesson page this also runs the lesson: one step
 * at a time, every word said aloud before anything is asked, and nothing
 * moves on while a voice is still speaking - the launcher answers /english/say
 * only once the line has been said.
 *
 * A wrong answer is never a dead end: the word is said again and the child
 * tries again. Only the first try of a step counts, and a word met for the
 * first time is not counted at all until it is found.
 */
(function () {
  "use strict";

  var finished = false;
  /* Set once the lesson is over: no more "active" reports after it (D-232). */

  function post(path, body, type) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": type || "application/json" },
      body: type ? body : JSON.stringify(body),
    })
      .then(function (response) {
        return response.json();
      })
      .catch(function () {
        return {};
      });
  }

  /* One line said, then the promise settles: English by the English voice,
   * anything else in the child's own language. */
  /* Each line said holds one of the page's six connections until it has
   * been spoken (D-254): a 🔊 tapped again while one still plays is let go,
   * or the taps after it - "home" too - would wait for a free one. */
  var saying = 0;
  var SAYING_MAX = 2;

  function say(text, english) {
    saying += 1;
    return post("/english/say", { text: text, lang: english ? "en" : "ui" }).then(
      function (result) {
        saying -= 1;
        return result;
      }
    );
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-say-en]");
    if (button && !button.closest("#lesson") && saying < SAYING_MAX) {
      say(button.getAttribute("data-say-en"), true);
    }
  });

  var dataNode = document.getElementById("lesson-data");
  var stage = document.getElementById("lesson");
  if (!dataNode || !stage) return;

  var lesson = JSON.parse(dataNode.textContent);
  var steps = lesson.steps;
  var strings = lesson.strings;
  var current = 0;
  var stars = 0;
  var busy = false;

  /* ------------------------------------------------------------ drawing */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, onTap) {
    var node = el("button", "big-btn " + (className || ""), label);
    node.type = "button";
    node.addEventListener("click", function () {
      if (!busy) onTap(node);
    });
    return node;
  }

  function cardView(card, big) {
    var view = el("div", "lesson-card" + (big ? " lesson-card--big" : ""));
    view.appendChild(el("div", "lesson-card__pic", card.pic));
    view.appendChild(el("div", "lesson-card__en", card.en));
    if (lesson.older && card.tr) view.appendChild(el("div", "lesson-card__tr", card.tr));
    return view;
  }

  function show(nodes) {
    stage.textContent = "";
    nodes.forEach(function (node) {
      stage.appendChild(node);
    });
    var bar = document.getElementById("lesson-progress");
    if (bar) bar.style.width = Math.round((current / steps.length) * 100) + "%";
  }

  function setStars(count) {
    stars = count;
    var node = document.getElementById("lesson-stars");
    if (node) node.textContent = String(stars);
  }

  function hold(promise) {
    busy = true;
    stage.classList.add("is-speaking");
    return Promise.resolve(promise).then(function (value) {
      busy = false;
      stage.classList.remove("is-speaking");
      return value;
    });
  }

  function praise() {
    var words = lesson.praise;
    return words[Math.floor(Math.random() * words.length)];
  }

  function answer(item, right) {
    if (lesson.practice) return Promise.resolve();
    return post("/english/answer", { item: item, right: right });
  }

  function next() {
    current += 1;
    if (current < steps.length) {
      run(steps[current]);
    } else {
      finish();
    }
  }

  /* -------------------------------------------------------- the steps */

  function run(step) {
    if (step.kind === "meet") return meet(step);
    if (step.kind === "find") return find(step);
    if (step.kind === "line") return line(step);
    if (step.kind === "story") return story(step);
    next();
  }

  /* A new word: seen, heard twice, said back. */
  function meet(step) {
    var row = el("div", "lesson-actions");
    row.appendChild(listenButton(step.card.en));
    row.appendChild(speakButton(step));
    row.appendChild(button(strings.next + " ▶", "big-btn--ok", next));
    show([el("p", "lesson-say", strings.new_word), cardView(step.card, true), row]);
    hold(
      say(strings.new_word, false)
        .then(function () {
          return say(step.card.en, true);
        })
        .then(function () {
          return say(step.card.en, true);
        })
    );
  }

  /* Hear a word, tap its picture. */
  function find(step) {
    var firstTry = true;
    var grid = el("div", "choices");
    step.choices.forEach(function (pic) {
      var choice = el("button", "choice", pic);
      choice.type = "button";
      choice.addEventListener("click", function () {
        if (busy) return;
        if (pic === step.card.pic) {
          choice.classList.add("is-right");
          setStars(stars + (firstTry ? 1 : 0));
          hold(
            Promise.all([answer(step.item, firstTry), say(praise(), true)]).then(function () {
              return say(step.card.en, true);
            })
          ).then(next);
        } else {
          firstTry = false;
          choice.classList.add("is-wrong");
          window.setTimeout(function () {
            choice.classList.remove("is-wrong");
          }, 600);
          hold(
            say(strings.wrong, false).then(function () {
              return say(step.card.en, true);
            })
          );
        }
      });
      grid.appendChild(choice);
    });
    var row = el("div", "lesson-actions");
    row.appendChild(listenButton(step.card.en));
    show([el("p", "lesson-say", strings.find), grid, row]);
    hold(
      say(strings.find, false).then(function () {
        return say(step.card.en, true);
      })
    );
  }

  /* A line: heard, then said back whole. */
  function line(step) {
    var row = el("div", "lesson-actions");
    row.appendChild(listenButton(step.card.en));
    row.appendChild(speakButton(step));
    row.appendChild(
      button(strings.said + " ✓", "big-btn--ok", function () {
        setStars(stars + 1);
        hold(answer(step.item, true)).then(next);
      })
    );
    show([el("p", "lesson-say", strings.line), cardView(step.card, false), row]);
    hold(
      say(strings.line, false).then(function () {
        return say(step.card.en, true);
      })
    );
  }

  /* A story: each page read aloud, then a question or two. */
  function story(step) {
    var page = 0;
    var allRight = true;

    function readPage() {
      var card = step.story[page];
      var row = el("div", "lesson-actions");
      row.appendChild(listenButton(card.en));
      row.appendChild(
        button(strings.next + " ▶", "big-btn--ok", function () {
          page += 1;
          if (page < step.story.length) readPage();
          else ask(0);
        })
      );
      show([el("p", "lesson-say", strings.story), cardView(card, true), row]);
      hold(say(card.en, true));
    }

    function ask(index) {
      if (index >= step.questions.length) {
        if (allRight) setStars(stars + 2);
        hold(answer(step.item, allRight)).then(next);
        return;
      }
      var question = step.questions[index];
      var firstTry = true;
      var grid = el("div", "choices");
      question.choices.forEach(function (pic) {
        var choice = el("button", "choice", pic);
        choice.type = "button";
        choice.addEventListener("click", function () {
          if (busy) return;
          if (pic === question.answer) {
            choice.classList.add("is-right");
            if (!firstTry) allRight = false;
            hold(say(praise(), true)).then(function () {
              ask(index + 1);
            });
          } else {
            firstTry = false;
            choice.classList.add("is-wrong");
            window.setTimeout(function () {
              choice.classList.remove("is-wrong");
            }, 600);
            hold(say(question.en, true));
          }
        });
        grid.appendChild(choice);
      });
      var prompt = el("div", "lesson-question");
      prompt.appendChild(el("div", "lesson-card__en", question.en));
      if (lesson.older) prompt.appendChild(el("div", "lesson-card__tr", question.tr));
      var row = el("div", "lesson-actions");
      row.appendChild(listenButton(question.en));
      show([el("p", "lesson-say", strings.question), prompt, grid, row]);
      hold(
        say(strings.question, false)
          .then(function () {
            return say(question.en, true);
          })
          .then(function () {
            // A four-year-old hears what was asked in their own words too.
            return lesson.older ? null : say(question.tr, false);
          })
      );
    }

    readPage();
  }

  function finish() {
    /* A lesson that is over is not learning, however long its last card stays
     * on the screen: the reports stop (D-232). */
    finished = true;
    var bar = document.getElementById("lesson-progress");
    if (bar) bar.style.width = "100%";
    var row = el("div", "lesson-actions");
    var home = el("a", "big-btn big-btn--quiet", "🔤 " + strings.home);
    home.href = "/english";
    var more = el("a", "big-btn", "🎲 " + strings.more);
    more.href = "/english/lesson?more=1";
    row.appendChild(home);
    row.appendChild(more);
    var done = strings.done.replace("{count}", String(stars));
    show([el("div", "lesson-finish", "🌟"), el("p", "lesson-say lesson-say--big", done), row]);
    hold(
      say(done, false).then(function () {
        return say(lesson.goodbye, true);
      })
    );
  }

  /* ------------------------------------------------- hearing and saying */

  function listenButton(text) {
    return button("🔊 " + strings.again, "big-btn--quiet", function () {
      if (saying < SAYING_MAX) hold(say(text, true));
    });
  }

  /* Held, not tapped, like the assistant's (D-166): down starts, up stops.
   * Only where the parent let the child speak to the assistant. */
  function speakButton(step) {
    if (!lesson.mic) return el("span");
    var node = button("🎤 " + strings.say, "big-btn--quiet lesson-mic", function () {});
    var recorder = null;
    var limit = 0;
    function start(event) {
      event.preventDefault();
      if (busy || recorder) return;
      recorder = record();
      node.classList.add("is-on");
      node.textContent = "🎙️ " + strings.listening;
      limit = window.setTimeout(stop, MAX_SECONDS * 1000);
    }
    function stop() {
      window.clearTimeout(limit);
      if (!recorder) return;
      var running = recorder;
      recorder = null;
      node.classList.remove("is-on");
      node.textContent = "🎤 " + strings.say;
      hold(
        running.stop().then(function (wav) {
          if (!wav) return null;
          return post("/english/hear?item=" + encodeURIComponent(step.item), wav, "audio/wav");
        })
      ).then(function (heard) {
        if (!heard) return;
        if (heard.match) {
          setStars(stars + 1);
          hold(say(strings.heard_ok, false));
        } else {
          hold(
            say(strings.heard_again, false).then(function () {
              return say(step.card.en, true);
            })
          );
        }
      });
    }
    node.addEventListener("pointerdown", start);
    ["pointerup", "pointerleave", "pointercancel"].forEach(function (name) {
      node.addEventListener(name, stop);
    });
    return node;
  }

  var SAMPLE_RATE = 16000;
  var MAX_SECONDS = 8;

  /* A recording in memory, as a 16-bit mono WAV: what the assistant's
   * press-and-speak sends, and kept nowhere (D-166). */
  function record() {
    var chunks = [];
    var stream = null;
    var context = null;
    var source = null;
    var processor = null;
    var stopped = false;
    var ready = navigator.mediaDevices
      .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (media) {
        stream = media;
        if (stopped) return;
        context = new AudioContext({ sampleRate: SAMPLE_RATE });
        source = context.createMediaStreamSource(stream);
        processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (event) {
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(processor);
        processor.connect(context.destination);
      })
      .catch(function () {
        say(strings.mic_failed, false);
      });

    function close() {
      if (processor) processor.disconnect();
      if (source) source.disconnect();
      if (stream) {
        stream.getTracks().forEach(function (track) {
          track.stop();
        });
      }
      if (context) context.close();
    }

    return {
      stop: function () {
        stopped = true;
        return ready.then(function () {
          var rate = context ? context.sampleRate : SAMPLE_RATE;
          close();
          var wav = encodeWav(chunks, rate);
          chunks = [];
          // Shorter than a third of a second: a tap, not a word.
          return wav.byteLength < 44 + rate * 0.3 * 2 ? null : wav;
        });
      },
    };
  }

  function encodeWav(parts, rate) {
    var length = 0;
    parts.forEach(function (part) {
      length += part.length;
    });
    var buffer = new ArrayBuffer(44 + length * 2);
    var view = new DataView(buffer);
    function text(offset, value) {
      for (var i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
    }
    text(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, length * 2, true);
    var offset = 44;
    parts.forEach(function (part) {
      for (var i = 0; i < part.length; i += 1) {
        var sample = Math.max(-1, Math.min(1, part[i]));
        view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
        offset += 2;
      }
    });
    return buffer;
  }

  /* ------------------------------------------------------------ minutes */

  /* While the page is in front, the corner's minutes are learning minutes:
   * the daemon trusts a report for ninety seconds, so every forty-five. */
  function active() {
    if (!finished && document.visibilityState === "visible") post("/english/active", {});
  }
  active();
  window.setInterval(active, 45000);

  run(steps[0]);
})();
