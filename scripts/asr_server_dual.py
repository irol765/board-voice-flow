#!/usr/bin/env python3
"""Dual-engine ASR server: strong transducer primary, SenseVoice fallback.

Drop-in successor for asr_server_sv.py with an identical HTTP contract:
  GET  /health     -> "ok model=dual ... counters ..."
  POST /transcribe -> body = wav bytes, response = plain-text transcript.

The primary is a sherpa-onnx zipformer transducer (zh-en). It is measurably
more accurate than SenseVoice on real voice notes (see SKILL.md, "Dual
engine"), but it has hard failure modes on a small minority of clips, so
SenseVoice stays resident as the fallback. Fallback triggers (each counted
in /health and, when ASR_FALLBACK_LOG is set, JSON-logged):
  crash   : the primary raised (e.g. a sherpa-onnx runtime error on a clip)
  empty   : the primary returned empty text (happens on some very short clips)
  timeout : the primary decode exceeded ASR_PRIMARY_TIMEOUT_S (default 20s)
On any trigger the SAME audio is decoded by SenseVoice and its text is
returned. If SenseVoice itself raises, the request fails with 500, exactly
like the single-engine server. Rollback is one step: point the service back
at asr_server_sv.py.

Env: ASR_SV_PORT (default 18765), ASR_XASR_MODEL_DIR, ASR_SV_MODEL_DIR,
     ASR_PRIMARY_TIMEOUT_S (20), ASR_FALLBACK_LOG (path or empty).
Requires: sherpa-onnx, numpy, and ffmpeg on PATH (normalization pass).
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import sherpa_onnx

HOST = "127.0.0.1"
PORT = int(os.environ.get("ASR_SV_PORT", "18765"))
SV_DIR = os.path.expanduser(os.environ.get(
    "ASR_SV_MODEL_DIR", "~/asr-models/sensevoice-small-int8"))
XASR_DIR = os.path.expanduser(os.environ.get(
    "ASR_XASR_MODEL_DIR", "~/asr-models/x-asr"))
PRIMARY_TIMEOUT = float(os.environ.get("ASR_PRIMARY_TIMEOUT_S", "20"))
FALLBACK_LOG = os.environ.get("ASR_FALLBACK_LOG", "")

print(f"[asr-dual] loading SenseVoice from {SV_DIR} ...", file=sys.stderr, flush=True)
SV = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(SV_DIR, "model.int8.onnx"),
    tokens=os.path.join(SV_DIR, "tokens.txt"),
    num_threads=2, language="zh", use_itn=False, debug=False)
print(f"[asr-dual] loading X-ASR from {XASR_DIR} ...", file=sys.stderr, flush=True)
XASR = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=os.path.join(XASR_DIR, "encoder-epoch-99-avg-1.int8.onnx"),
    decoder=os.path.join(XASR_DIR, "decoder-epoch-99-avg-1.onnx"),
    joiner=os.path.join(XASR_DIR, "joiner-epoch-99-avg-1.int8.onnx"),
    tokens=os.path.join(XASR_DIR, "tokens.txt"),
    num_threads=4, decoding_method="greedy_search", debug=False)
print("[asr-dual] both models ready", file=sys.stderr, flush=True)

LOCK = threading.Lock()
EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xasr")
COUNTERS = {"requests": 0, "primary_ok": 0, "fallback_crash": 0,
            "fallback_empty": 0, "fallback_timeout": 0, "sv_error": 0}
CLOCK = threading.Lock()


def bump(key):
    with CLOCK:
        COUNTERS[key] += 1


def log_fallback(reason, nbytes, detail=""):
    bump("fallback_" + reason)
    row = {"ts": time.strftime("%F %T"), "event": "fallback",
           "reason": reason, "bytes": nbytes, "detail": detail[:200]}
    line = json.dumps(row, ensure_ascii=False)
    sys.stderr.write("[asr-dual] " + line + "\n")
    sys.stderr.flush()
    if FALLBACK_LOG:
        with open(FALLBACK_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def decode_wav_bytes(data: bytes):
    """PCM16 wav bytes -> (sample_rate, float32 samples in [-1, 1])."""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got sampwidth={sw}")
    if len(raw) % 2:
        raw = raw[:-1]
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch)[:, 0]
    return sr, samples


def to_16k(sr, samples):
    """Normalize every clip with ffmpeg before decoding (16 kHz mono +
loudness normalization). The accuracy comparison this design rests on was
measured with this pass in the loop, so the production path keeps it
byte-faithful to the measured setup. If you remove it, re-measure."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        with wave.open(tf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        tmp_in = tf.name
    tmp_out = tmp_in + ".16k.wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", tmp_in,
                        "-ar", "16000", "-ac", "1", tmp_out],
                       check=True, timeout=120)
        return decode_wav_bytes(open(tmp_out, "rb").read())
    finally:
        for p in (tmp_in, tmp_out):
            if os.path.exists(p):
                os.unlink(p)


def xasr_decode(data: bytes) -> str:
    sr, samples = decode_wav_bytes(data)
    sr, samples = to_16k(sr, samples)
    s = XASR.create_stream()
    s.accept_waveform(sr, samples)
    XASR.decode_stream(s)
    return s.result.text.strip()


def sv_decode(data: bytes) -> str:
    sr, samples = decode_wav_bytes(data)
    s = SV.create_stream()
    s.accept_waveform(sr, samples)
    SV.decode_stream(s)
    return s.result.text.strip()


def transcribe_bytes(data: bytes) -> str:
    bump("requests")
    reason = None
    detail = ""
    fut = EXEC.submit(xasr_decode, data)
    try:
        text = fut.result(timeout=PRIMARY_TIMEOUT)
        if text:
            bump("primary_ok")
            return text
        reason = "empty"
    except FutTimeout:
        reason = "timeout"
        detail = f">{PRIMARY_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        reason = "crash"
        detail = f"{type(e).__name__}: {e}"
    log_fallback(reason, len(data), detail)
    return sv_decode(data)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[asr-dual] %s\n" % (fmt % args))
        sys.stderr.flush()

    def _send_text(self, code: int, text: str):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            with CLOCK:
                c = dict(COUNTERS)
            self._send_text(200, "ok model=dual xasr=zipformer-epoch99 "
                                 f"sv=sensevoice-small counters={json.dumps(c)}")
        else:
            self._send_text(404, "not found")

    def do_POST(self):
        if self.path != "/transcribe":
            self._send_text(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_text(400, "empty body")
            return
        data = self.rfile.read(length)
        try:
            with LOCK:
                text = transcribe_bytes(data)
        except Exception as e:  # noqa: BLE001
            bump("sv_error")
            print(f"[asr-dual] transcribe failed: {e!r}", file=sys.stderr, flush=True)
            self._send_text(500, f"error: {e}")
            return
        self._send_text(200, text)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[asr-dual] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()
