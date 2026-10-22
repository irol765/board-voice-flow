# Board Voice Flow

A Muse skill + tiny local pipeline that makes voice notes from a small voice
terminal ("board") reliable: every note is transcribed **locally** by a resident
SenseVoice service, appended to one transcripts file, and the assistant answers
by comparing the platform auto-transcript against the local one.

Why: the platform auto-transcript of short, quiet voice notes is often garbled
or even in the wrong language. A local SenseVoice-Small model is fast
(~0.3–0.7 s per note on 2 CPU cores, ~350 MB RSS), private (nothing leaves
127.0.0.1), and in practice far more accurate for Chinese/English mixed speech.

中文一句话：给语音小终端加一条本地转写流水线——语音一落盘就用本地
SenseVoice 转准稿存进一个文件，助手比对平台稿与准稿后再回复。

## What's inside

```
SKILL.md                  The skill: architecture, setup, reply flow, safety rules
skill.yaml                Skill descriptor (drop the folder into custom-skills/)
scripts/asr_server_sv.py  Resident local ASR HTTP service (127.0.0.1:18765)
scripts/asr_watch.py      Watcher: new voice_note-*.wav -> transcribe -> board-transcripts.txt
scripts/asr_guard.sh      Health guard: units + /health + transcript freshness, self-recovers
scripts/guard_watch.sh    Zero-token poller: logs the guard's line, escalates only ALERT
systemd/asr-server.service
systemd/asr-watch.service Boot-time units for both processes
systemd/asr-guard.service
systemd/asr-guard.timer   Runs the health guard every 5 minutes
LICENSE                   MIT (code and skill text)
```

## Fastest path: hand it to your Muse

Copy one block, paste it to your own Muse, and it deploys everything and
verifies the install for you:

```text
Please set up the board-voice-flow local transcription pipeline for me. The repo is https://github.com/irol765/board-voice-flow — follow its README and SKILL.md:
1. Install SKILL.md and skill.yaml as the skill board_voice_flow in my custom-skills folder, and deploy the two scripts and systemd units from the repo.
2. Download the SenseVoice-Small int8 model (link in the README, ~230 MB) and create the venv with sherpa-onnx.
3. Enable both services (asr-server, asr-watch) at boot, install the health guard (scripts/asr_guard.sh, scripts/guard_watch.sh and the asr-guard systemd timer, every 5 minutes), then verify with one existing voice_note wav: its line must appear in board-transcripts.txt within a few seconds.
4. From then on, handle the voice-note files that arrive in my main chat by the SKILL.md flow: read the platform transcript, compare with the local transcript file, and trust the local one.
Adapt paths to this machine, and report the verification result when done.
```

中文版（复制这一段发给你的 Muse 即可）：

```text
请帮我装 board-voice-flow 本地语音转写流水线，仓库：https://github.com/irol765/board-voice-flow ，照它的 README 和 SKILL.md 做：
1. 把 SKILL.md 和 skill.yaml 装进我的 custom-skills（技能名 board_voice_flow），脚本和 systemd 服务模板照仓库部署；
2. 下载 SenseVoice-Small int8 模型（README 里有链接，约 230MB），建 venv 装好 sherpa-onnx；
3. 把 asr-server 和 asr-watch 两个服务设成开机自启，并装上健康守护（scripts/asr_guard.sh、scripts/guard_watch.sh 与 asr-guard systemd 定时器，每 5 分钟一轮），再用一条现成的 voice_note 语音验收：转写稿几秒内要出现在 board-transcripts.txt 里；
4. 以后处理主聊里收到的语音文件（voice_note）按 SKILL.md 流程：先看平台转写、再对本地准稿，以本地准稿为准。
路径按我这台机器的实际调整，装完把验收结果告诉我。
```

## Quick start (manual)

1. Download the model (SenseVoice-Small int8 for sherpa-onnx, ~230 MB) — the
   weights are **not** in this repo:

   ```bash
   wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
   tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
   # you need model.int8.onnx and tokens.txt
   ```

2. Create a venv and install the runtime:

   ```bash
   python3 -m venv asr-sv-venv
   asr-sv-venv/bin/pip install sherpa-onnx numpy
   ```

3. Copy the two scripts into your workspace tools directory, adjust the paths
   at the top of each, and install the two systemd units (adjust `HOME` and
   paths inside them first). Enable both:

   ```bash
   systemctl enable --now asr-server asr-watch
   curl http://127.0.0.1:18765/health
   ```

   Then install the health guard the same way: copy `scripts/asr_guard.sh`
   and `scripts/guard_watch.sh` next to them, keep a copy of the unit files
   in the tools `systemd/` directory (the guard restores vanished units
   from there), install `systemd/asr-guard.service` and
   `systemd/asr-guard.timer`, and enable the timer:

   ```bash
   systemctl enable --now asr-guard.timer
   ```

4. Copy one existing voice note to a test id (e.g. `voice_note-9999.wav`) in
   the watched folder and confirm its line shows up in `board-transcripts.txt`
   within a few seconds. Delete the test file and line afterwards.

5. Give the skill to your Muse: copy this folder into its
   `~/workspace/custom-skills/` directory. `SKILL.md` contains the full reply
   flow — compare-first answering, one-line summaries for slow tasks, and the
   safety rule that board voice is an unauthenticated channel.

## Health guard

The pipeline's failure mode is silence — a dead ASR server or watcher just
means transcripts stop, with nothing visibly broken. A plain bash guard
(`scripts/asr_guard.sh`) therefore runs every 5 minutes on a systemd timer:
it checks both units (present, enabled, active — restoring a vanished unit
file from the kept copies), probes `/health` (one restart, then re-check),
and checks transcript freshness (an untranscribed note older than 240 s
restarts the watcher), plus the unresolved-failures flag the watcher keeps
for notes its automatic retry could not recover. It prints one line —
`OK`, `RECOVERED` or `ALERT` —
and a zero-token poller (`scripts/guard_watch.sh`) logs the line and only
escalates an `ALERT` (the guard could not fix it) to the agent, once per
distinct alert and at most hourly. No LLM patrols the healthy path; see
SKILL.md, "Health guard", for the full contract.

## Measured on the reference setup

- 2 vCPU / 8 GB VM, model loaded once, transcription serialized by a lock
- New note to transcript line in the file: ~3 s end to end (2 s poll + ~0.5 s ASR)
- Watcher is stdlib-only Python; the ASR service uses only the standard
  library HTTP stack plus sherpa-onnx

## Where this comes from

This pipeline was built for — and battle-tested on — the
[Mu酱 / Muchan](https://musejiang.com) voice-terminal gadget, a small
ESP32-S3 AMOLED device that talks to Muse through a custom relay channel.
If you are here because your gadget's voice input keeps coming back garbled,
the Mu酱 site is also where the flashing tool and firmware live.

## Versions

- **1.1.1 (2026-10-22)** — race fix: the watcher now waits for `/health`
  before transcribing after a (re)start and defers notes while the service
  is unhealthy, instead of marking them `[FAILED]` against a still-loading
  model. `[FAILED]` entries get one automatic retry after health recovers
  (the line is rewritten in place on success); entries that still fail are
  listed in `asr-failed.flag`, which the guard reports as an `ALERT`.
- **1.1.0 (2026-10-22)** — adds the health guard layer: `scripts/asr_guard.sh`
  (unit presence + `/health` + transcript-freshness checks with
  self-recovery), `scripts/guard_watch.sh` (zero-token poller that only
  escalates `ALERT`), and the `asr-guard` systemd timer (every 5 minutes).
  SKILL.md gains a "Health guard" section. The transcription core and the
  reply flow are unchanged.
- **1.0.0** — initial release: resident SenseVoice transcription pipeline
  (`asr_server_sv.py` + `asr_watch.py`), systemd units, and the
  compare-first reply flow.

## Credits and licenses

- Code and skill text in this repo: MIT (see `LICENSE`).
- ASR runtime: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0).
- Model: SenseVoice-Small by Alibaba FunASR, distributed via sherpa-onnx
  releases under its own license. Download it from the link above; it is not
  redistributed here.
