/* Recording a parent's own voice for Zio and Sio's lines (D-174).
 *
 * Press once to start, once to stop. The page builds a mono 16-bit WAV itself -
 * no MediaRecorder, whose formats differ between phones and none of which
 * pw-play reads - and sends it to the panel, which checks it before keeping it.
 * Nothing leaves the machine but the request to the panel itself.
 *
 * Without this file the page still lists what is recorded and can play and
 * delete it; only recording needs JavaScript, because the microphone does.
 */
(function () {
  "use strict";

  var MAX_SECONDS = 14;
  var strings = window.BIOKIDS_VOICE_STRINGS || {};
  var csrf = document.querySelector('meta[name="csrf-token"]');
  csrf = csrf ? csrf.getAttribute("content") : "";

  if (!navigator.mediaDevices || !window.AudioContext) {
    document.querySelectorAll("[data-record]").forEach(function (button) {
      button.disabled = true;
      button.title = strings.unsupported || "";
    });
    return;
  }

  var active = null;

  function stopTracks(state) {
    if (state.processor) state.processor.disconnect();
    if (state.source) state.source.disconnect();
    if (state.stream) {
      state.stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    if (state.context) state.context.close();
  }

  function start(button) {
    var state = { button: button, chunks: [] };
    active = state;
    navigator.mediaDevices
      .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) {
        state.stream = stream;
        state.context = new AudioContext();
        state.source = state.context.createMediaStreamSource(stream);
        state.processor = state.context.createScriptProcessor(4096, 1, 1);
        state.processor.onaudioprocess = function (event) {
          state.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        state.source.connect(state.processor);
        state.processor.connect(state.context.destination);
        button.classList.add("is-recording");
        button.textContent = strings.stop || "■";
        state.limit = window.setTimeout(function () {
          stop(state);
        }, MAX_SECONDS * 1000);
      })
      .catch(function () {
        active = null;
        status(button, strings.mic_failed || "");
      });
  }

  function stop(state) {
    if (active !== state) return;
    active = null;
    window.clearTimeout(state.limit);
    var rate = state.context ? state.context.sampleRate : 48000;
    stopTracks(state);
    var button = state.button;
    button.classList.remove("is-recording");
    button.textContent = strings.record || "●";
    var wav = encodeWav(downsample(state.chunks, rate, 24000), Math.min(rate, 24000));
    status(button, strings.saving || "");
    var sentence = document.getElementById(button.getAttribute("data-text"));
    var url =
      button.getAttribute("data-record") +
      "?text=" + encodeURIComponent(sentence ? sentence.value : "");
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "audio/wav", "X-CSRF-Token": csrf },
      body: wav,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (result) {
        if (result && result.ok) {
          window.location.reload();
        } else {
          status(button, (result && result.error) || strings.failed || "");
        }
      })
      .catch(function () {
        status(button, strings.failed || "");
      });
  }

  function status(button, text) {
    var target = document.getElementById(button.getAttribute("data-status"));
    if (target) target.textContent = text;
  }

  /* Phones record at 44.1 or 48 kHz; speech needs half that, and the file is
   * a quarter of the size. Averaging is enough for a voice. */
  function downsample(parts, from, to) {
    if (from <= to) return parts;
    var ratio = from / to;
    var total = 0;
    parts.forEach(function (part) {
      total += part.length;
    });
    var joined = new Float32Array(total);
    var at = 0;
    parts.forEach(function (part) {
      joined.set(part, at);
      at += part.length;
    });
    var out = new Float32Array(Math.floor(total / ratio));
    for (var i = 0; i < out.length; i += 1) {
      var begin = Math.floor(i * ratio);
      var end = Math.min(total, Math.floor((i + 1) * ratio));
      var sum = 0;
      for (var j = begin; j < end; j += 1) sum += joined[j];
      out[i] = end > begin ? sum / (end - begin) : 0;
    }
    return [out];
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

  document.querySelectorAll("[data-record]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (active && active.button === button) {
        stop(active);
      } else if (!active) {
        start(button);
      }
    });
  });
})();
