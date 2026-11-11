#!/usr/bin/env python3
"""voice_gate: transcript gate - push new local transcripts to the agent.

Compares three things:
- <files dir>/voice_note-<n>.wav    incoming voice notes
- <files dir>/board-transcripts.txt local ASR transcripts (n<TAB>time<TAB>text)
- a state file                      note numbers already delivered

Modes:
- check   read-only: print JSON {new:[{num,text}], missing:[num...]} -
          new = transcript exists but not yet delivered,
          missing = a wav older than GRACE seconds still has no transcript
- deliver like check, and record the new numbers in the state file
          (this is the mode a fast hook/cron runs, e.g. every 5 seconds)
- status  human-readable state: newest wav, newest transcript, delivered up to

Env: BVF_FILES_DIR (default ~/voice-notes),
     BVF_GATE_STATE (default ~/.voice-gate-state.json)
"""
import fcntl
import glob
import json
import os
import re
import sys
import time

FILES = os.path.expanduser(os.environ.get("BVF_FILES_DIR", "~/voice-notes"))
TRANSCRIPTS = os.path.join(FILES, "board-transcripts.txt")
STATE = os.path.expanduser(os.environ.get("BVF_GATE_STATE", "~/.voice-gate-state.json"))
GRACE = 180  # grace seconds after a wav lands before its missing transcript is reported

def load_transcripts():
    out = {}
    try:
        with open(TRANSCRIPTS, encoding="utf-8") as f:
            for line in f:
                if not line.endswith("\n"):
                    continue  # trailing line may be mid-append: a partial line is not a transcript yet
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit():
                    out[int(parts[0])] = parts[2]
    except FileNotFoundError:
        pass
    return out

def load_wavs():
    out = {}
    for p in glob.glob(os.path.join(FILES, "voice_note-*.wav")):
        m = re.search(r"voice_note-(\d+)\.wav$", p)
        if m:
            out[int(m.group(1))] = os.path.getmtime(p)
    return out

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return set(json.load(f).get("delivered", []))
    except Exception:
        return None  # no state file = never ran

def save_state(delivered):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp%d" % os.getpid()  # unique temp name: overlapping runs must not clobber each other
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"delivered": sorted(delivered)}, f)
    os.replace(tmp, STATE)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    lock_f = None
    if mode == "deliver":
        # hold an exclusive lock across read-modify-write: a 5 s cadence can overlap itself
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        lock_f = open(STATE + ".lock", "w")
        fcntl.flock(lock_f, fcntl.LOCK_EX)
    tr = load_transcripts()
    wavs = load_wavs()
    delivered = load_state()
    now = time.time()
    if mode == "status":
        print(json.dumps({
            "latest_wav": max(wavs) if wavs else None,
            "latest_transcript": max(tr) if tr else None,
            "delivered_up_to": max(delivered) if delivered else None,
            "wav_count": len(wavs),
            "transcript_count": len(tr),
        }, ensure_ascii=False))
        return
    if delivered is None:
        # first run: baseline - mark all existing transcripts delivered, report the fact, never flood history
        if mode == "deliver":
            save_state(set(tr))
        print(json.dumps({"seeded": True, "baseline_up_to": max(tr) if tr else None,
                          "new": [], "missing": []}, ensure_ascii=False))
        return
    new = [{"num": n, "text": tr[n]} for n in sorted(tr) if n not in delivered]
    missing = [n for n in sorted(wavs) if n not in tr and now - wavs[n] > GRACE]
    print(json.dumps({"new": new, "missing": missing}, ensure_ascii=False))
    if mode == "deliver" and new:
        save_state(delivered | {item["num"] for item in new})

if __name__ == "__main__":
    main()
