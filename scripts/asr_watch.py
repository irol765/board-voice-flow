#!/usr/bin/env python3
"""板子语音自动转写看守：新 wav 一落盘就调本机 SenseVoice 转写，结果追加到
~/workspace/user/files/board-transcripts.txt，主会话扫一眼文件即可。

- 每 2 秒扫 ~/workspace/user/files/voice_note-<数字>.wav
- 幂等：以 board-transcripts.txt 已记录的编号为准，已记录的永不重转
- 文件尚在写入的保护：同一文件大小连续两次扫描不变（且 >0）才转
- 首次启动（记录文件为空）时：只补转已存在的最近 10 条，更早的存量跳过并记日志，
  避免把上百条历史灌满文件；之后新到/漏转的都转
- 输出行格式：<编号>\t<YYYY-MM-DD HH:MM:SS>\t<转写原文>；失败为 [FAILED]
- 竞速防护（WK-86）：启动时、以及转写失败且服务不健康时，先等 /health 返回
  ok 再处理积压——模型加载窗口内不硬转，避免把可转的语音标成 [FAILED]
- [FAILED] 补转（WK-86）：服务恢复健康后，已标 [FAILED] 的条目自动补转
  一次（重试记录持久化在 .asr-failed-retry.json，重启不重复补）；补转仍
  失败的编号写 asr-failed.flag，守护（vm-state-guard）据此报 ALERT
日志：~/workspace/tools/asr_watch.log（轮转 1MB×3）
"""
import glob
import http.client
import json
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
HEALTH_WAIT_MAX = 300  # 等 /health 的上限（秒）：模型加载远快于此
FAILED_FLAG = os.path.join(FILES_DIR, "asr-failed.flag")
RETRY_STATE = os.path.join(FILES_DIR, ".asr-failed-retry.json")

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


def health_ok():
    conn = http.client.HTTPConnection(ASR_HOST, ASR_PORT, timeout=3)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except Exception:
        return False
    finally:
        conn.close()


def wait_health():
    """等 ASR 服务 /health 返回 200（服务进程在模型加载完才开端口，
    所以 health 通 = 模型可用）。返回是否等到。"""
    t0 = time.time()
    while time.time() - t0 < HEALTH_WAIT_MAX:
        if health_ok():
            return True
        time.sleep(2)
    return health_ok()


def load_retry_state():
    try:
        with open(RETRY_STATE, encoding="utf-8") as f:
            j = json.load(f)
        return set(j.get("retried", [])), set(j.get("unresolved", []))
    except Exception:
        return set(), set()


def save_retry_state(retried, unresolved):
    # Disk is the source of truth for retry state: an automatic save must
    # never drop an unresolved entry that is on disk and still [FAILED]
    # (a restart or a second process holding stale in-memory state would
    # otherwise overwrite it). An entry leaves the unresolved set on its
    # own only when its transcript line is no longer [FAILED]. Manual
    # handling edits the state file directly; retry_failed re-reads the
    # disk every round, so manual edits are honored, never resurrected.
    try:
        _, disk_unresolved = load_retry_state()
    except Exception:
        disk_unresolved = set()
    merged = set(unresolved) | set(disk_unresolved)
    if os.path.exists(OUT_FILE):
        merged &= set(collect_failed_ids())
    unresolved = merged
    tmp = RETRY_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"retried": sorted(retried), "unresolved": sorted(unresolved)}, f)
    os.replace(tmp, RETRY_STATE)
    if unresolved:
        with open(FAILED_FLAG, "w", encoding="utf-8") as f:
            f.write("\n".join(str(n) for n in sorted(unresolved)) + "\n")
    elif os.path.exists(FAILED_FLAG):
        os.unlink(FAILED_FLAG)


def collect_failed_ids():
    failed = []
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[0].isdigit() and parts[2] == "[FAILED]":
                    failed.append(int(parts[0]))
    return failed


def rewrite_line(num, text):
    """把 OUT_FILE 中 num 的 [FAILED] 行原地替换为转写结果（原子重写）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(OUT_FILE, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = []
    hit = False
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[0] == str(num) and parts[2] == "[FAILED]":
            out.append(f"{num}\t{ts}\t{text}\n")
            hit = True
        else:
            out.append(line)
    if hit:
        tmp = OUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out)
        os.replace(tmp, OUT_FILE)
    return hit


def retry_failed(retried, unresolved):
    """[FAILED] 条目在服务健康后自动补转一次。补转仍失败进 unresolved
    并经 asr-failed.flag 浮给守护报 ALERT，不再静默。"""
    if not health_ok():
        return
    # Every round starts from the on-disk state (refreshed in place, so
    # callers' set references stay valid): a manual fix or another
    # process's update is never overwritten by this process's stale copy.
    disk_retried, disk_unresolved = load_retry_state()
    retried.clear(); retried.update(disk_retried)
    unresolved.clear(); unresolved.update(disk_unresolved)
    wavs = list_wavs()
    changed = False
    for num in collect_failed_ids():
        if num in retried:
            continue
        path = wavs.get(num)
        if path is None:
            continue  # wav 已删，无从补转；不占重试名额
        if not health_ok():
            break  # 服务中途又不健康：整轮推迟，不消耗任何名额
        retried.add(num)
        changed = True
        try:
            text = transcribe(path)
            if rewrite_line(num, text):
                unresolved.discard(num)
                log.info("retry %d OK -> %d chars", num, len(text))
        except Exception as e:
            if not health_ok():
                # 失败源于服务掉线而非条目本身：退回名额，下轮再试
                retried.discard(num)
                log.warning("retry %d aborted (service unhealthy): %s", num, e)
                break
            unresolved.add(num)
            log.error("retry %d FAILED again: %s (flagged for guard ALERT)", num, e)
    if changed:
        save_retry_state(retried, unresolved)


def main():
    if not wait_health():
        log.error("ASR health not up after %ds at startup; proceeding anyway",
                  HEALTH_WAIT_MAX)
    retried, unresolved = load_retry_state()
    retry_failed(retried, unresolved)
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
            deferred = False
            new_failed = False
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
                    if not health_ok():
                        # 服务不健康（多半在加载模型）：不标 FAILED、不占
                        # done，等健康恢复后自然重转本条与积压。
                        log.warning("transcribe %d deferred (service unhealthy): %s",
                                    num, e)
                        deferred = True
                        break
                    append_line(num, "[FAILED]")
                    log.error("transcribe %d FAILED: %s", num, e)
                    new_failed = True
                done.add(num)
                sizes.pop(num, None)
            if deferred:
                wait_health()
                retry_failed(retried, unresolved)
                done = load_done_ids()
            elif new_failed:
                retry_failed(retried, unresolved)
            # 清理已消失文件的 size 记录
            for num in list(sizes):
                if num not in wavs:
                    sizes.pop(num, None)
        except Exception as e:
            log.error("scan loop error: %s", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
