/* Sesim (D-240): hold the big button to record, let go to keep it.
 *
 * Recorded in the page as mono 16-bit WAV, the way the assistant's
 * press-and-speak is (assistant.js), and posted to the launcher, which keeps
 * it in the child's own home. Play, send to a grown-up, and delete are one
 * tap each; delete asks once, because a story a child is proud of should not
 * vanish under a stray finger. No text the child must read to use it: the
 * button changes colour and counts the seconds while it listens.
 */
(function () {
  "use strict";

  var page = document.getElementById("sesim");
  if (!page) return;
  var strings = {};
  try {
    strings = JSON.parse(page.getAttribute("data-strings") || "{}");
  } catch (err) {
    strings = {};
  }
  var MAX_SECONDS = Number(page.getAttribute("data-max-seconds")) || 40;
  var SAMPLE_RATE = 16000;

  var record = document.getElementById("sesim-record");
  var label = document.getElementById("sesim-label");
  var timer = document.getElementById("sesim-timer");
  var message = document.getElementById("sesim-message");
  var audio = document.getElementById("sesim-audio");

  var stream = null;
  var context = null;
  var source = null;
  var processor = null;
  var chunks = [];
  var started = 0;
  var ticking = null;
  var limit = null;
  var busy = false;
  var held = false;
  /* The finger is on the button. The microphone takes a moment to open; a
   * finger lifted before it does must not leave it recording (D-240). */

  function say(text, tone) {
    if (!message) return;
    message.textContent = text || "";
    message.hidden = !text;
    message.setAttribute("data-tone", tone || "info");
  }

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

  function showTime() {
    var seconds = Math.min(MAX_SECONDS, Math.floor((Date.now() - started) / 1000));
    timer.textContent = seconds + " / " + MAX_SECONDS;
  }

  var opening = false;

  function startRecording() {
    /* `opening`: a second press while the microphone is being opened must not
     * open another that nothing would close (D-250). */
    if (busy || stream || opening) return;
    opening = true;
    chunks = [];
    say("");
    window.clearTimeout(limit);
    window.clearInterval(ticking);
    navigator.mediaDevices
      .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (media) {
        opening = false;
        if (!held) {
          media.getTracks().forEach(function (track) {
            track.stop();
          });
          say(strings.too_short, "warn");
          return;
        }
        stream = media;
        context = new AudioContext({ sampleRate: SAMPLE_RATE });
        source = context.createMediaStreamSource(stream);
        processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (event) {
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(processor);
        processor.connect(context.destination);
        started = Date.now();
        record.classList.add("is-on");
        label.textContent = strings.listening || "";
        timer.hidden = false;
        showTime();
        ticking = window.setInterval(showTime, 250);
        limit = window.setTimeout(stopRecording, MAX_SECONDS * 1000);
      })
      .catch(function () {
        opening = false;
        stopTracks();
        say(strings.mic_failed, "warn");
      });
  }

  function stopRecording() {
    window.clearTimeout(limit);
    window.clearInterval(ticking);
    record.classList.remove("is-on");
    label.textContent = strings.hold || "";
    timer.hidden = true;
    if (!stream) return;
    var rate = context ? context.sampleRate : SAMPLE_RATE;
    stopTracks();
    var wav = encodeWav(chunks, rate);
    chunks = [];
    if (wav.byteLength < 44 + rate * 0.5 * 2) {
      say(strings.too_short, "warn");
      return;
    }
    busy = true;
    fetch("/sesim", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (result) {
        busy = false;
        if (result && result.ok) {
          window.location.replace("/sesim?saved=1");
        } else {
          say((result && result.message) || strings.failed, "warn");
        }
      })
      .catch(function () {
        busy = false;
        say(strings.failed, "warn");
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
  record.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    held = true;
    startRecording();
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach(function (name) {
    record.addEventListener(name, function () {
      held = false;
      if (stream) stopRecording();
    });
  });
  record.addEventListener("keydown", function (event) {
    if ((event.key === " " || event.key === "Enter") && !stream && !held) {
      held = true;
      startRecording();
    }
  });
  record.addEventListener("keyup", function (event) {
    if (event.key === " " || event.key === "Enter") {
      held = false;
      if (stream) stopRecording();
    }
  });

  function post(path) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(function (response) {
      return response.json();
    });
  }

  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target.closest("button") : null;
    if (!target) return;
    var name = target.getAttribute("data-play");
    if (name) {
      audio.src = "/sesim/file/" + encodeURIComponent(name);
      audio.play().catch(function () {});
      return;
    }
    name = target.getAttribute("data-share");
    if (name) {
      target.disabled = true;
      post("/sesim/" + encodeURIComponent(name) + "/share")
        .then(function (result) {
          say(result && result.ok ? strings.shared : strings.share_failed, result && result.ok ? "ok" : "warn");
        })
        .catch(function () {
          say(strings.share_failed, "warn");
        })
        .then(function () {
          target.disabled = false;
        });
      return;
    }
    name = target.getAttribute("data-delete");
    if (name) {
      if (target.getAttribute("data-armed") !== "1") {
        /* Once to arm, again to delete: a story should not vanish by mistake. */
        target.setAttribute("data-armed", "1");
        target.textContent = "❓";
        window.setTimeout(function () {
          target.setAttribute("data-armed", "0");
          target.textContent = "🗑️";
        }, 3000);
        return;
      }
      post("/sesim/" + encodeURIComponent(name) + "/delete").then(function () {
        window.location.replace("/sesim");
      });
    }
  });

  if (window.location.search.indexOf("saved=1") !== -1) say(strings.saved, "ok");
})();
