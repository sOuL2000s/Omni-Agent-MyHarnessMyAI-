# agent/stt.py
"""Local, streaming speech-to-text.

Uses sherpa-onnx for the recognizer (fast, CPU-only, no GPU needed) and
sounddevice for audio capture. Both are optional: if they aren't
installed, `is_available()` returns False and the /api/stt/* endpoints
return a clear error string. The rest of Omni works without them.

Model download
--------------
Download a streaming model from the sherpa-onnx release page:

    https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models

Recommended (small, English, streaming):
    sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2

Extract it somewhere and point `AGENT_STT_MODEL_DIR` at the extracted
folder. The folder should contain `encoder-*.onnx`, `decoder-*.onnx`,
`joiner-*.onnx`, and `tokens.txt` (or the encoder/decoder-only variant
for transducer models).

Multilingual:
    sherpa-onnx-streaming-zipformer-multi-zh-hans-2023-12-12

Smaller, lower quality (fast on old CPUs):
    sherpa-onnx-streaming-zipformer-en-20M-2023-02-17
"""
import os
import queue
import threading
import time
from pathlib import Path


# ---- optional imports ------------------------------------------------------
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

try:
    import sherpa_onnx
    HAS_SHERPA = True
except ImportError:
    HAS_SHERPA = False


SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_MS = 100         # read 100ms chunks from the mic
ENDPOINT_SILENCE_MS = 400


def _model_dir() -> Path:
    """Where to look for the ONNX model.

    Precedence: AGENT_STT_MODEL_DIR env > data/stt-model/ > ~/.omni/stt-model/.
    """
    env = os.environ.get("AGENT_STT_MODEL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    # Two standard locations. The first is per-install, the second is
    # shared across installs of the same user.
    for cand in (Path("data") / "stt-model", Path.home() / ".omni" / "stt-model"):
        if cand.is_dir():
            return cand
    return Path("data") / "stt-model"


def _find_model_files(d: Path) -> dict:
    """Return {'encoder': path, 'decoder': path, 'joiner': path, 'tokens': path}
    for the first complete model we find in `d`.

    Supports the two main sherpa-onnx layouts:
      * transducer:  encoder-*.onnx, decoder-*.onnx, joiner-*.onnx
      * paraformer:  model.onnx, tokens.txt
    """
    if not d.is_dir():
        return {}
    files = list(d.glob("*.onnx"))
    tokens = list(d.glob("tokens.txt"))
    # Transducer layout (encoder/decoder/joiner)
    enc = next((f for f in files if f.name.startswith("encoder")), None)
    dec = next((f for f in files if f.name.startswith("decoder")), None)
    joi = next((f for f in files if f.name.startswith("joiner")), None)
    if enc and dec and joi and tokens:
        return {"encoder": enc, "decoder": dec, "joiner": joi, "tokens": tokens[0]}
    # Paraformer / offline-style layout
    model = next((f for f in files if f.name.startswith("model")), None)
    if model and tokens:
        return {"model": model, "tokens": tokens[0]}
    return {}


def is_available() -> tuple:
    """Return (ok: bool, reason: str).

    `ok=True` means the recognizer can be constructed. The reason string
    is user-facing; the /api/stt/status endpoint returns it verbatim.
    """
    if not HAS_NUMPY:
        return False, "numpy is not installed (pip install numpy)"
    if not HAS_SHERPA:
        return False, "sherpa-onnx is not installed (pip install sherpa-onnx)"
    if not HAS_SOUNDDEVICE:
        return False, "sounddevice is not installed (pip install sounddevice)"
    d = _model_dir()
    if not d.is_dir():
        return False, f"model directory not found: {d}. Set AGENT_STT_MODEL_DIR."
    files = _find_model_files(d)
    if not files:
        return False, f"no model files in {d} (need encoder-*.onnx/decoder-*.onnx/joiner-*.onnx + tokens.txt)"
    return True, str(d)


# ---- recognizer construction ----------------------------------------------
def _make_recognizer(model_dir: Path):
    """Build a sherpa_onnx.OnlineRecognizer for the files in `model_dir`."""
    files = _find_model_files(model_dir)
    if not files:
        raise RuntimeError(f"no model files in {model_dir}")
    common = dict(
        tokens=str(files["tokens"]),
        num_threads=2,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        # Endpoint detection: 400ms of silence ends the current utterance.
        rule1_min_trailing_silence=ENDPOINT_SILENCE_MS / 1000.0,
        rule2_min_trailing_silence=1.2,      # no words for 1.2s -> end
        rule3_min_utterance_length=20.0,     # hard cap on a single utterance
    )
    if "encoder" in files:
        # Transducer (zipformer) — the standard streaming layout.
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(files["encoder"]),
            decoder=str(files["decoder"]),
            joiner=str(files["joiner"]),
            **common,
        )
    # Paraformer / offline-style
    return sherpa_onnx.OnlineRecognizer.from_paraformer(
        paraformer=str(files["model"]),
        **common,
    )


# ---- live session ----------------------------------------------------------
class LiveSession:
    """A single microphone capture + recognizer session.

    Feed `start()` to begin, `stop()` to stop. Transcript chunks are
    appended to `self.events` as dicts: {"type": "partial"|"final",
    "text": "...", "t": float}. The caller polls `events` via
    `drain()`.

    A single session captures from the default input device. Multiple
    concurrent sessions are not supported (would need two mics); the
    App guards against that with a lock.
    """

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.events = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._recognizer = None
        self._stream_state = None
        self._last_partial = ""
        self._error = None

    def start(self):
        self._recognizer = _make_recognizer(self.model_dir)
        self._stream_state = self._recognizer.create_stream()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name="stt-capture")
        self._thread.start()

    def _run(self):
        q = queue.Queue()
        block = int(SAMPLE_RATE * BLOCK_MS / 1000)

        def _cb(indata, frames, time_info, status):
            # Called from the PortAudio thread. Copy the buffer; do NOT
            # do recognition work here (it would block the audio thread
            # and cause dropouts).
            if status:
                # Some statuses (input overflow) are recoverable.
                pass
            q.put(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="float32", blocksize=block, callback=_cb,
            )
            self._stream.start()
        except Exception as e:
            self._error = f"could not open microphone: {e}"
            self._push("final", self._error)
            return

        last_partial_emit = 0.0
        try:
            while not self._stop.is_set():
                try:
                    block_in = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                samples = block_in[:, 0]  # mono
                self._stream_state.accept_waveform(SAMPLE_RATE, samples)
                while self._recognizer.is_ready(self._stream_state):
                    self._recognizer.decode_stream(self._stream_state)
                text = self._recognizer.get_result(self._stream_state).strip()
                now = time.time()
                if text and text != self._last_partial:
                    # Throttle partials to ~8/sec so the UI doesn't
                    # thrash on every decode.
                    if now - last_partial_emit > 0.12:
                        self._last_partial = text
                        last_partial_emit = now
                        self._push("partial", text)
                if self._recognizer.is_endpoint(self._stream_state):
                    if text:
                        self._push("final", text)
                    self._last_partial = ""
                    self._recognizer.reset(self._stream_state)
        except Exception as e:
            self._error = f"recognizer failed: {e}"
            self._push("final", self._error)
        finally:
            try:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
            except Exception:
                pass
            self._push("closed", "")

    def _push(self, kind, text):
        with self.lock:
            self.events.append({"type": kind, "text": text, "t": time.time()})
            # Cap the buffer so a long session doesn't grow unbounded.
            if len(self.events) > 2000:
                del self.events[:len(self.events) - 2000]

    def drain(self) -> list:
        with self.lock:
            out = self.events
            self.events = []
            return out

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()