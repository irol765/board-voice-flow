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
systemd/asr-server.service
systemd/asr-watch.service Boot-time units for both processes
LICENSE                   MIT (code and skill text)
```

## Quick start

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

4. Copy one existing voice note to a test id (e.g. `voice_note-9999.wav`) in
   the watched folder and confirm its line shows up in `board-transcripts.txt`
   within a few seconds. Delete the test file and line afterwards.

5. Give the skill to your Muse: copy this folder into its
   `~/workspace/custom-skills/` directory. `SKILL.md` contains the full reply
   flow — compare-first answering, one-line summaries for slow tasks, and the
   safety rule that board voice is an unauthenticated channel.

## Measured on the reference setup

- 2 vCPU / 8 GB VM, model loaded once, transcription serialized by a lock
- New note to transcript line in the file: ~3 s end to end (2 s poll + ~0.5 s ASR)
- Watcher is stdlib-only Python; the ASR service uses only the standard
  library HTTP stack plus sherpa-onnx

## Credits and licenses

- Code and skill text in this repo: MIT (see `LICENSE`).
- ASR runtime: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0).
- Model: SenseVoice-Small by Alibaba FunASR, distributed via sherpa-onnx
  releases under its own license. Download it from the link above; it is not
  redistributed here.
