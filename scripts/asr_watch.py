#!/usr/bin/env python3
"""板子语音自动转写看守：新 wav 一落盘就调本机 SenseVoice 转写，结果追加到
~/workspace/user/files/board-transcripts.txt，主会话扫一眼文件即可。

- 每 2 秒扫 ~/workspace/user/files/voice_note-<数字>.wav
- 幂等：以 board-transcripts.txt 已记录的编号为准，已记录的永不重转
- 文件尚在写入的保护：同一文件大小连续两次扫描不变（且 >0）才转
- 首次启动（记录文件为空）时：只补转已存在的最近 10 条，更早的存量跳过并记日志，
  避免把上百条历史灌满文件；之后新到/漏转的都转
- 输出行格式：<编号>\t<YYYY-MM-DD HH:MM:SS>\t<转写原文>；失败为 [FAILED]
日志：~/workspace/tools/asr_watch.log（轮转 1MB×3）
"""
import glob
import http.client
import logging
import logging.handlers
import os
import re
import time
from datetime import datetime

# 按脚本位置推导（~/workspace/tools/asr_watch.py），不依赖 $HOME（systemd 下会变 /root）
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.join(WORKSPACE, "user/files")
OUT_FILE = os.path.join(FILES_DIR, "board-transcripts.txt")
LOG_FILE = os.path.join(WORKSPACE, "tools/asr_watch.log")
ASR_HOST, ASR_PORT = "127.0.0.1", 18765
POLL_SEC = 2
BASELINE_RECENT = 10  # 首次启动时补转的存量条数

NAME_RE = re.compile(r"^voice_note-(\d+)\.wav$")

log = logging.getLogger("asr_watch")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)


def load_done_ids():
    done = set()
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                head = line.split("\t", 1)[0].strip()
                if head.isdigit():
                    done.add(int(head))
    return done


def list_wavs():
    out = {}
    for path in glob.glob(os.path.join(FILES_DIR, "voice_note-*.wav")):
        m = NAME_RE.match(os.path.basename(path))
        if m:
            out[int(m.group(1))] = path
    return out


def transcribe(path):
    with open(path, "rb") as f:
        data = f.read()
    conn = http.client.HTTPConnection(ASR_HOST, ASR_PORT, timeout=120)
    try:
        conn.request("POST", "/transcribe", body=data,
                     headers={"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace").strip()
        if resp.status != 200 or not body:
            raise RuntimeError(f"http {resp.status} empty={not body}")
        return " ".join(body.split())  # 单行化
    finally:
        conn.close()


def append_line(num, text):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(f"{num}\t{ts}\t{text}\n")


def main():
    done = load_done_ids()
    wavs = list_wavs()
    fresh_start = not done
    baseline_cutoff = None
    if fresh_start and len(wavs) > BASELINE_RECENT:
        ids_sorted = sorted(wavs)
        baseline_cutoff = ids_sorted[-BASELINE_RECENT]
        skipped = len(ids_sorted) - BASELINE_RECENT
        log.info("fresh start: baseline cutoff id>=%d, skipping %d older files",
                 baseline_cutoff, skipped)
    log.info("asr_watch started: done=%d existing_wavs=%d fresh=%s",
             len(done), len(wavs), fresh_start)

    sizes = {}  # num -> last seen size (stability check)
    while True:
        try:
            wavs = list_wavs()
            for num in sorted(wavs):
                if num in done:
                    continue
                if baseline_cutoff is not None and num < baseline_cutoff:
                    done.add(num)  # 存量老文件：本轮启动内不再反复判断
                    continue
                path = wavs[num]
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size == 0 or sizes.get(num) != size:
                    sizes[num] = size  # 第一次见或还在变，等下一轮确认稳定
                    continue
                # 大小连续两轮不变 -> 转写
                try:
                    text = transcribe(path)
                    append_line(num, text)
                    log.info("transcribed %d (%d bytes wav) -> %d chars",
                             num, size, len(text))
                except Exception as e:
                    append_line(num, "[FAILED]")
                    log.error("transcribe %d FAILED: %s", num, e)
                done.add(num)
                sizes.pop(num, None)
            # 清理已消失文件的 size 记录
            for num in list(sizes):
                if num not in wavs:
                    sizes.pop(num, None)
        except Exception as e:
            log.error("scan loop error: %s", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
