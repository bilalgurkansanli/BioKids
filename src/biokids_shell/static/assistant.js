/* The assistant screen (D-165, D-166, D-216).
 *
 * A question goes to /assistant/ask as JSON. What comes back is either the end
 * of it - a refusal, which is a sentence people wrote - or the start of an
 * answer, with an id the page asks /assistant/more about twice a second until
 * the answer is done. "Düşünüyorum…" stays until the first sentence; then each
 * sentence is added to Zio's bubble as it arrives and read aloud in turn, one
 * at a time, through /assistant/say, which answers once the sentence has been
 * said. Every sentence has been through the word list before it gets here.
 *
 * Press-and-speak records only while the button is held - at most fifteen
 * seconds - and stops the microphone the moment it is released, so Chromium's
 * own "recording" indicator goes out with it. The recording is turned into a
 * 16 kHz WAV in memory, posted once, and dropped.
 *
 * On /english/chat the same page is a conversation in English with Sio
 * (D-226): Sio speaks first, every message carries the last few said, Sio's
 * sentences are read in English in his voice, the microphone hears English,
 * and the words under the box can be tapped as an answer.
 */
(function () {
  "use strict";

  var MAX_SECONDS = 15;
  var SAMPLE_RATE = 16000;
  var SLOW_MS = 6000;
  var POLL_MS = 500;
  var MAX_POLLS = 600; /* five minutes; every service ends an answer long before */
  var MAX_MISSES = 3;

  var strings = JSON.parse(document.getElementById("assistant-strings").textContent);
  var form = document.getElementById("assistant-form");
  var input = document.getElementById("assistant-question");
  var send = document.getElementById("assistant-send");
  var log = document.getElementById("assistant-log");
  var mic = document.getElementById("assistant-mic");
  var listening = document.getElementById("assistant-listening");
  var character = document.querySelector(".assistant__character svg");
  var busy = false;

  var modeNode = document.getElementById("assistant-mode");
  var chat = modeNode ? JSON.parse(modeNode.textContent) : {};
  var english = chat.mode === "english";
  var speaker = english ? "sio" : "zio";
  /* What was said, oldest first, as [who, text]: Sio remembers the last few. */
  var history = [];
  var HISTORY = 8;

  function bubble(text, who) {
    var item = document.createElement("li");
    item.className = "bubble bubble--" + who;
    item.textContent = text;
    log.appendChild(item);
    item.scrollIntoView({ block: "end", behavior: "smooth" });
    return item;
  }

  function setBusy(on) {
    busy = on;
    input.disabled = on;
    send.disabled = on;
    if (mic) mic.disabled = on;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  /* ---------------------------------------------------------------- speech */

  /* Sentences waiting for Zio, in order. The next is sent only once the last
   * has been said, so two never overlap and the voice service's short queue
   * never fills with an answer while a time warning needs a place in it. */
  var unsaid = [];
  var saying = false;

  /* `lang`: the answer's language, Turkish or English, which Zio says it in
   * (D-244); none for a sentence of the launcher's own, said in the child's. */
  function say(text, lang) {
    unsaid.push({ text: text, lang: lang || "" });
    sayNext();
  }

  function sayNext() {
    if (saying || !unsaid.length) return;
    saying = true;
    if (character) character.classList.add("is-speaking");
    var next = unsaid.shift();
    var line = { text: next.text };
    if (next.lang) line.lang = next.lang;
    if (english) line.mode = "english";
    post("/assistant/say", line)
      .catch(function () {})
      .then(function () {
        saying = false;
        if (!unsaid.length && character) character.classList.remove("is-speaking");
        sayNext();
      });
  }

  /* A child who missed something taps it and hears it again (D-245): a
   * sentence of an answer, or the whole bubble when the tap is beside the
   * words. What was still waiting to be said gives way to it. */
  log.addEventListener("click", function (event) {
    var item = event.target.closest ? event.target.closest(".bubble--zio, .bubble--sio") : null;
    if (!item || item.classList.contains("bubble--thinking")) return;
    var sentence = event.target.closest("span[lang], .bubble > span");
    var parts = sentence && item.contains(sentence) ? [sentence] : item.querySelectorAll("span");
    unsaid = [];
    if (!parts.length) {
      say(item.textContent);
    } else {
      Array.prototype.forEach.call(parts, function (part) {
        say(part.textContent.trim(), part.getAttribute("lang") || "");
      });
    }
    item.classList.add("is-replaying");
    window.setTimeout(function () {
      item.classList.remove("is-replaying");
    }, 600);
  });

  /* ---------------------------------------------------------------- asking */

  function ask(question) {
    question = (question || "").trim();
    if (!question || busy) return;
    bubble(question, "child");
    input.value = "";
    unsaid = []; /* what is still unsaid of the last answer is not said now */
    setBusy(true);
    var answer = bubble(strings.thinking, speaker);
    var said = [];
    answer.classList.add("bubble--thinking");
    var shown = 0;
    var answerLang = ""; /* the language the answer is in (D-244) */
    var polls = 0;
    var misses = 0;
    var slow = window.setTimeout(function () {
      if (!shown) answer.textContent = strings.loading;
    }, SLOW_MS);

    function add(text, aloud, lang) {
      if (!shown) {
        answer.textContent = "";
        answer.classList.remove("bubble--thinking");
      }
      var sentence = document.createElement("span");
      sentence.textContent = (shown ? " " : "") + text;
      if (lang) sentence.setAttribute("lang", lang);
      answer.appendChild(sentence);
      shown += 1;
      said.push(text);
      answer.scrollIntoView({ block: "end", behavior: "smooth" });
      if (aloud) say(text, lang);
    }

    function finish(ending, refused, aloud) {
      window.clearTimeout(slow);
      if (english && !refused && said.length) {
        history.push(["child", question], ["sio", said.join(" ")]);
        history = history.slice(-HISTORY);
      }
      if (ending) {
        if (refused && !shown) answer.classList.add("bubble--refused");
        add(ending, aloud);
      } else if (!shown) {
        add(strings.heard_nothing, false);
      }
      setBusy(false);
      input.focus();
    }

    function take(reply) {
      if (reply.lang && !answerLang) answerLang = reply.lang;
      (reply.sentences || []).forEach(function (text) {
        add(text, true, answerLang);
      });
      if (reply.done || !reply.id) {
        finish(reply.ending, reply.refused, true);
      } else if (++polls > MAX_POLLS) {
        finish(strings.resting, true, true);
      } else {
        window.setTimeout(function () {
          more(reply.id);
        }, POLL_MS);
      }
    }

    function more(id) {
      post("/assistant/more", { id: id })
        .then(function (reply) {
          misses = 0;
          take(reply);
        })
        .catch(function () {
          if (++misses >= MAX_MISSES) {
            finish(strings.mic_failed, true, false);
          } else {
            window.setTimeout(function () {
              more(id);
            }, POLL_MS);
          }
        });
    }

    var asked = { question: question };
    if (english) {
      asked.mode = "english";
      asked.history = history.slice(-HISTORY);
    }
    post("/assistant/ask", asked)
      .then(take)
      .catch(function () {
        finish(strings.mic_failed, true, false);
      });
  }

  /* Load the model now: a child takes about as long to type a question as the
   * model takes to wake up (D-165). Nothing waits for the answer. */
  fetch("/assistant/warmup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(function () {});

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ask(input.value);
  });

  /* A word tapped is an answer, for a child who cannot type it yet. */
  var chips = document.getElementById("assistant-chips");
  if (chips) {
    chips.addEventListener("click", function (event) {
      var chip = event.target.closest("[data-chip]");
      if (chip) ask(chip.getAttribute("data-chip"));
    });
  }

  /* Sio speaks first, in English, about the words the child is learning. */
  if (english && chat.opening) {
    bubble(chat.opening, "sio");
    say(chat.opening);
    history.push(["sio", chat.opening]);
  }

  /* --------------------------------------------------------- press-and-speak */

  /* D-245. Held, the microphone records - at most fifteen seconds - and the
   * screen shows it hears the child: bars that move with their voice and the
   * seconds left. Let go, the recording is turned into words, which come into
   * the box to be checked, the way a grown-up's dictation does: the child can
   * hear their own recording, send the words, or delete them. A recording in
   * which nothing was heard at all says so, and points to the microphone. */

  if (!mic) return;

  var voice = JSON.parse(
    (document.getElementById("assistant-voice-strings") || {}).textContent || "{}"
  );
  var level = document.getElementById("assistant-level");
  var bars = level ? level.querySelectorAll("i") : [];
  var left = document.getElementById("assistant-left");
  var draft = document.getElementById("assistant-draft");
  var draftStatus = document.getElementById("assistant-draft-status");
  var play = document.getElementById("assistant-play");
  var discard = document.getElementById("assistant-discard");
  var SILENT_RMS = 0.01; /* the loudest moment quieter than this: nothing came in */

  var stream = null;
  var context = null;
  var source = null;
  var processor = null;
  var analyser = null;
  var chunks = [];
  var limit = null;
  var ticker = null;
  var frame = null;
  var holding = false;
  /* Asked for, not yet given: a second press then must not open a second
   * microphone that nothing would ever close (D-250). */
  var opening = false;
  var startedAt = 0;
  var loudest = 0;
  var recording = null; /* an object URL of the last recording, for "Sesimi dinle" */
  var player = null;

  function stopTracks() {
    if (frame) window.cancelAnimationFrame(frame);
    if (processor) processor.disconnect();
    if (analyser) analyser.disconnect();
    if (source) source.disconnect();
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    if (context) context.close();
    stream = context = source = processor = analyser = frame = null;
  }

  function forgetRecording() {
    if (player) player.pause();
    if (recording) URL.revokeObjectURL(recording);
    recording = player = null;
  }

  function showDraft(text, canPlay) {
    if (!draft) return;
    draftStatus.textContent = text;
    if (play) play.hidden = !canPlay;
    draft.hidden = false;
  }

  function closeDraft() {
    if (draft) draft.hidden = true;
    send.classList.remove("is-ready");
    forgetRecording();
  }

  function drawLevel() {
    if (!analyser) return;
    var samples = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(samples);
    var sum = 0;
    for (var i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    var rms = Math.sqrt(sum / samples.length);
    loudest = Math.max(loudest, rms);
    /* Speech is quiet as numbers go: a third of full scale fills the bars. */
    var height = Math.min(1, rms * 3.5);
    for (var b = 0; b < bars.length; b += 1) {
      var shape = 1 - Math.abs(b - (bars.length - 1) / 2) / bars.length;
      bars[b].style.transform =
        "scaleY(" + Math.max(0.12, height * shape * (0.75 + Math.random() * 0.5)) + ")";
    }
    frame = window.requestAnimationFrame(drawLevel);
  }

  function showSecondsLeft() {
    if (!left) return;
    var remaining = Math.max(0, MAX_SECONDS - Math.floor((Date.now() - startedAt) / 1000));
    left.textContent = remaining;
  }

  function startRecording() {
    if (busy || stream || opening) return;
    opening = true;
    closeDraft();
    chunks = [];
    loudest = 0;
    window.clearTimeout(limit);
    window.clearInterval(ticker);
    navigator.mediaDevices
      .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (media) {
        opening = false;
        if (!holding) {
          /* Let go before the microphone was ready: a tap, not a question. */
          media.getTracks().forEach(function (track) {
            track.stop();
          });
          showDraft(voice.hold || strings.heard_nothing, false);
          return;
        }
        stream = media;
        context = new AudioContext({ sampleRate: SAMPLE_RATE });
        source = context.createMediaStreamSource(stream);
        analyser = context.createAnalyser();
        analyser.fftSize = 1024;
        processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (event) {
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(analyser);
        source.connect(processor);
        processor.connect(context.destination);
        startedAt = Date.now();
        listening.hidden = false;
        mic.classList.add("is-on");
        showSecondsLeft();
        ticker = window.setInterval(showSecondsLeft, 250);
        drawLevel();
        limit = window.setTimeout(stopRecording, MAX_SECONDS * 1000);
      })
      .catch(function (error) {
        opening = false;
        stopTracks();
        var missing = error && (error.name === "NotFoundError" || error.name === "NotReadableError");
        showDraft(missing ? voice.none || strings.mic_failed : strings.mic_failed, false);
      });
  }

  function stopRecording() {
    holding = false;
    window.clearTimeout(limit);
    window.clearInterval(ticker);
    listening.hidden = true;
    mic.classList.remove("is-on");
    if (!stream) return;
    var rate = context ? context.sampleRate : SAMPLE_RATE;
    stopTracks();
    var wav = encodeWav(chunks, rate);
    chunks = [];
    if (wav.byteLength < 44 + rate * 0.3 * 2) {
      showDraft(voice.hold || strings.heard_nothing, false);
      return;
    }
    recording = URL.createObjectURL(new Blob([wav], { type: "audio/wav" }));
    if (loudest < SILENT_RMS) {
      /* Nothing at all came in: no use asking Whisper about silence. */
      showDraft(voice.silent || strings.heard_nothing, true);
      return;
    }
    showDraft(voice.working || strings.thinking, true);
    setBusy(true);
    fetch(english ? "/assistant/hear?lang=en" : "/assistant/hear", {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (heard) {
        setBusy(false);
        if (heard.text) {
          input.value = heard.text;
          send.classList.add("is-ready");
          showDraft(voice.check || "", true);
        } else {
          showDraft(strings.heard_nothing, true);
        }
      })
      .catch(function () {
        setBusy(false);
        showDraft(strings.mic_failed, true);
      });
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

  /* Held, not tapped: down starts, up stops - wherever the finger is by then,
   * since the button keeps the pointer it was pressed with. */
  mic.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    if (mic.setPointerCapture) {
      try {
        mic.setPointerCapture(event.pointerId);
      } catch (err) {
        /* an old pointer id: the button still works without it */
      }
    }
    holding = true;
    startRecording();
  });
  ["pointerup", "pointercancel"].forEach(function (name) {
    mic.addEventListener(name, stopRecording);
  });
  mic.addEventListener("contextmenu", function (event) {
    event.preventDefault(); /* a long press on a touch screen is not a menu */
  });

  if (play) {
    play.addEventListener("click", function () {
      if (!recording) return;
      if (player) player.pause();
      player = new Audio(recording);
      player.play().catch(function () {});
    });
  }
  if (discard) {
    discard.addEventListener("click", function () {
      input.value = "";
      closeDraft();
    });
  }
  /* What is sent, or typed over, leaves the draft behind. */
  form.addEventListener("submit", closeDraft);
  window.addEventListener("pagehide", forgetRecording);
})();
