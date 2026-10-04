/* The assistant screen (D-165, D-166).
 *
 * A question goes to /assistant/ask as JSON and comes back as the sentence to
 * show; the launcher has already asked Zio to say it aloud. Press-and-speak
 * records only while the button is held - at most fifteen seconds - and stops
 * the microphone the moment it is released, so Chromium's own "recording"
 * indicator goes out with it. The recording is turned into a 16 kHz WAV in
 * memory, posted once, and dropped.
 */
(function () {
  "use strict";

  var MAX_SECONDS = 15;
  var SAMPLE_RATE = 16000;
  var SLOW_MS = 6000;

  var strings = JSON.parse(document.getElementById("assistant-strings").textContent);
  var form = document.getElementById("assistant-form");
  var input = document.getElementById("assistant-question");
  var send = document.getElementById("assistant-send");
  var log = document.getElementById("assistant-log");
  var mic = document.getElementById("assistant-mic");
  var listening = document.getElementById("assistant-listening");
  var character = document.querySelector(".assistant__character svg");
  var busy = false;

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
    if (character) character.classList.toggle("is-speaking", false);
  }

  function ask(question) {
    question = (question || "").trim();
    if (!question || busy) return;
    bubble(question, "child");
    input.value = "";
    setBusy(true);
    var thinking = bubble(strings.thinking, "zio");
    thinking.classList.add("bubble--thinking");
    var slow = window.setTimeout(function () {
      thinking.textContent = strings.loading;
    }, SLOW_MS);

    fetch("/assistant/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (reply) {
        thinking.classList.remove("bubble--thinking");
        thinking.classList.toggle("bubble--refused", !!reply.refused);
        thinking.textContent = reply.text || strings.heard_nothing;
        if (character) {
          character.classList.add("is-speaking");
          window.setTimeout(function () {
            character.classList.remove("is-speaking");
          }, Math.min(12000, 1500 + (reply.text || "").length * 70));
        }
      })
      .catch(function () {
        thinking.classList.remove("bubble--thinking");
        thinking.textContent = strings.mic_failed;
      })
      .finally(function () {
        window.clearTimeout(slow);
        setBusy(false);
        input.focus();
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

  /* --------------------------------------------------------- press-and-speak */

  if (!mic) return;

  var stream = null;
  var context = null;
  var source = null;
  var processor = null;
  var chunks = [];
  var limit = null;

  function stopTracks() {
    if (processor) processor.disconnect();
    if (source) source.disconnect();
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    if (context) context.close();
    stream = context = source = processor = null;
  }

  function startRecording() {
    if (busy || stream) return;
    chunks = [];
    navigator.mediaDevices
      .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (media) {
        stream = media;
        context = new AudioContext({ sampleRate: SAMPLE_RATE });
        source = context.createMediaStreamSource(stream);
        processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (event) {
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(processor);
        processor.connect(context.destination);
        listening.hidden = false;
        mic.classList.add("is-on");
        limit = window.setTimeout(stopRecording, MAX_SECONDS * 1000);
      })
      .catch(function () {
        stopTracks();
        bubble(strings.mic_failed, "zio");
      });
  }

  function stopRecording() {
    window.clearTimeout(limit);
    listening.hidden = true;
    mic.classList.remove("is-on");
    if (!stream) return;
    var rate = context ? context.sampleRate : SAMPLE_RATE;
    stopTracks();
    var wav = encodeWav(chunks, rate);
    chunks = [];
    if (wav.byteLength < 44 + rate * 0.3 * 2) {
      return; // shorter than a third of a second: a tap, not a question
    }
    setBusy(true);
    fetch("/assistant/hear", {
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
          ask(heard.text);
        } else {
          bubble(strings.heard_nothing, "zio");
        }
      })
      .catch(function () {
        setBusy(false);
        bubble(strings.mic_failed, "zio");
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

  /* Held, not tapped: down starts, up (or leaving the button) stops. */
  mic.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    startRecording();
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach(function (name) {
    mic.addEventListener(name, stopRecording);
  });
})();
