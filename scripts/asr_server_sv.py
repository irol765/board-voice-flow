#!/usr/bin/env python3
"""本地转写常驻服务：SenseVoice-Small int8 (sherpa-onnx)，模型只加载一次。

- 只绑 127.0.0.1，标准库 http.server，无第三方 web 框架。接口与旧版一致：
  GET  /health     -> "ok model=sensevoice-small ..."
  POST /transcribe -> 请求体为 wav 原始字节，响应为纯文本转写结果一行。
- 解码走标准库 wave 路径（PCM16 直读），不依赖 PyAV。
- 端口默认 18765，可用环境变量 ASR_SV_PORT 覆盖（自测时用 18766）。

启动: ~/workspace/tools/asr-sv-venv/bin/python ~/workspace/tools/asr_server_sv.py
"""
import io
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import sherpa_onnx

HOST = "127.0.0.1"
PORT = int(os.environ.get("ASR_SV_PORT", "18765"))
MODEL_DIR = os.path.expanduser("~/workspace/tools/asr-models/sensevoice-small-int8")
MODEL_PATH = os.path.join(MODEL_DIR, "model.int8.onnx")
TOKENS_PATH = os.path.join(MODEL_DIR, "tokens.txt")

print(f"[asr-sv] loading model={MODEL_PATH} ...", file=sys.stderr, flush=True)
RECOGNIZER = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=MODEL_PATH,
    tokens=TOKENS_PATH,
    num_threads=2,
    language="zh",
    use_itn=False,
    debug=False,
)
print("[asr-sv] model ready", file=sys.stderr, flush=True)

LOCK = threading.Lock()  # 2 核机器：转写串行，避免并发抢 CPU/内存


def decode_wav_bytes(data: bytes):
    """PCM16 wav 字节 -> (sample_rate, float32 samples in [-1, 1])。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got sampwidth={sw}")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch)[:, 0]
    return sr, samples


def transcribe_bytes(data: bytes) -> str:
    sr, samples = decode_wav_bytes(data)
    stream = RECOGNIZER.create_stream()
    stream.accept_waveform(sr, samples)
    RECOGNIZER.decode_stream(stream)
    return stream.result.text.strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 日志走 stderr（进 asr_server_sv.log）
        sys.stderr.write("[asr-sv] %s\n" % (fmt % args))
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
            self._send_text(200, f"ok model=sensevoice-small engine=sherpa-onnx dir={MODEL_DIR}")
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
            print(f"[asr-sv] transcribe failed: {e!r}", file=sys.stderr, flush=True)
            self._send_text(500, f"error: {e}")
            return
        self._send_text(200, text)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[asr-sv] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()
