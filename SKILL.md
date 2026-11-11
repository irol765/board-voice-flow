# Board Voice Pipeline

Use this skill when the user talks to you through a small voice terminal (a "board") that sends voice notes into the chat, and the platform's built-in auto-transcript is unreliable (garbled text, wrong language, dropped words). It sets up a local transcription pipeline and the reply flow that goes with it.

## What this achieves

- Give every incoming voice note a trustworthy local transcript within seconds, without sending audio to a cloud ASR service.

- Keep all local transcripts in one append-only file so you can ground each reply in the accurate text instead of guessing from the platform transcript.

- Keep board replies short and immediate, and route slow or long results to whichever channel the user actually reads afterwards (a follow-up board message by default, the main chat history when that is their reading place).

## Architecture

- Run a resident local ASR server: SenseVoice-Small (sherpa-onnx, int8 build) behind a tiny HTTP service that binds 127.0.0.1 only. Reference implementation: `scripts/asr_server_sv.py` (endpoints: `GET /health`, `POST /transcribe` with the raw wav bytes as the body, plain-text response). Expect roughly 0.3–0.7 s per short note and about 350 MB RSS once the model is loaded.

- Run a watcher next to it: poll the directory where voice-note files land (`voice_note-<id>.wav`) every 2 seconds, transcribe each new file through the local server, and append one line per note to a transcripts file. Reference implementation: `scripts/asr_watch.py`, output format `<id>\t<timestamp>\t<transcript>` in `board-transcripts.txt` in the same directory as the wav files.

- Make the watcher idempotent: never re-transcribe an id already recorded in the transcripts file, and only transcribe a file after its size is unchanged across two polls, so a half-written upload is never read.

- On first start, backfill only the most recent 10 existing notes so the file is useful immediately without being flooded by history.

- Write a `[FAILED]` line for a note whose transcription failed, so a failure is visible in the file instead of silently missing.

- Never transcribe against a still-loading model: the watcher waits for `GET /health` to answer before processing anything after a (re)start, and when a transcription fails while the service is unhealthy it defers the note instead of marking it `[FAILED]`. (The server opens its port only after the model is loaded, so health answering means the model is ready.)

- Retry `[FAILED]` entries once, automatically, after health recovers: the entry's line is rewritten in place with the real transcript. The retry is recorded persistently, so restarts do not retry forever. An entry that still fails after its retry is listed in `asr-failed.flag` next to the transcripts file — the health guard turns a non-empty flag into an `ALERT` (below), so an unrecoverable note surfaces instead of staying silent.

### Dual engine (recommended)

- Run both models in one process (`scripts/asr_server_dual.py`, a drop-in replacement for `asr_server_sv.py` with the identical HTTP contract): a sherpa-onnx zipformer transducer (zh-en) as the primary and SenseVoice as the fallback. The watcher does not change.

- Why not just swap models: the transducer is measurably more accurate on real traffic — in a three-day shadow run against the production engine on 150 real notes, it won the decisive subset 28 to 15 with about 21% fewer character errors — but it has hard failure modes SenseVoice does not: it crashed outright on 2 of those 150 clips and returned empty text on some very short ones. A clean swap turns those into `[FAILED]` notes. Keeping the old engine as the fallback deletes that failure mode while keeping the accuracy win.

- Fallback triggers, each counted in `/health` and optionally JSON-logged (`ASR_FALLBACK_LOG`): the primary raised an exception (crash), returned empty text (empty), or exceeded the decode timeout (timeout, default 20 s). On any trigger the same audio is decoded by SenseVoice and its text returned. If the fallback itself raises, the request fails with a 500, exactly like the single-engine server.

- Evaluate before you switch, the same way: shadow-run the candidate against your current engine on your own recent notes for a few days, adjudicate the differences, and look at the failure census, not just the accuracy win. Switch only on a decisive win, and keep the previous engine as the fallback rather than retiring it.

- Rollback is one step: point the service back at `asr_server_sv.py`.

## Setup

- Prepare the model once: download the SenseVoice-Small int8 sherpa-onnx model (`model.int8.onnx` plus `tokens.txt`) into a tools directory, and create a Python venv with `sherpa-onnx` and `numpy` installed.

- For the dual engine (recommended): also download a sherpa-onnx zipformer transducer model (zh-en) and install `ffmpeg` (the dual server normalizes every clip with it — the accuracy numbers above were measured with that pass in the loop). Point the `asr-server` unit's `ExecStart` at `scripts/asr_server_dual.py` instead, with `ASR_XASR_MODEL_DIR` and `ASR_SV_MODEL_DIR` naming the two model directories, and confirm `GET /health` answers `model=dual` after a restart.

- Copy `scripts/asr_server_sv.py` and `scripts/asr_watch.py` into the workspace tools directory and adjust the paths at the top of each script to the local layout. The watcher derives the workspace from its own file location; the server resolves the model directory from the user's home.

- Install both systemd units from `systemd/` as system services and enable them, so the pipeline survives reboots: `asr-server.service` for the ASR server, `asr-watch.service` for the watcher.

- Set `Environment=HOME=/home/<user>` in the ASR server unit. Under systemd the home directory resolves to /root, and any script that expands `~` will look for the model in the wrong place without this.

- Verify the setup end to end before relying on it: `GET /health` answers ok, then copy any existing voice note to a throwaway id (for example `voice_note-9999.wav`) in the watched directory and confirm its line appears in the transcripts file within about 5 seconds with correct text. Delete the test file and its line afterwards.

## Health guard (a script, not an agent)

- The pipeline fails silently when it fails: a dead ASR server or watcher just means new lines stop appearing in the transcripts file, and nobody notices until the user does. Guard it with a plain script on a timer — **never with an agent cron or any LLM patrol**. An agent that wakes every few minutes to read "OK" burns budget all day and still reacts slower than the script does.

- The guard (`scripts/asr_guard.sh`) checks four things on every run, in this order: (1) both systemd units — the unit file is present (a vanished file, e.g. after a VM rebuild, is reinstalled from the kept copies in the tools `systemd/` directory), the unit is enabled, and the service is active, restarting one that is down; (2) the ASR server's `GET /health` — on failure it restarts `asr-server` once and re-checks; (3) transcript freshness — if the newest voice note's id is ahead of the transcripts file and that note has waited longer than 240 seconds, it restarts `asr-watch`; (4) the unresolved-failures flag (`asr-failed.flag`) — a non-empty flag means the watcher's automatic retry could not recover those notes, and the guard reports them as an `ALERT`.

- The guard recovers what it can by itself and prints exactly one line per run: `OK`, `RECOVERED` (it fixed something), or `ALERT` (it could not), followed by details. A poller (`scripts/guard_watch.sh`, run by `systemd/asr-guard.timer` every 5 minutes) appends every line to a state log and stays silent on `OK` and `RECOVERED` — a successful self-recovery is not news.

- **The guard must outlive the failure it guards against.** The systemd timer that schedules the guard lives in the same system directories a host or VM replacement wipes — so on an ephemeral host, one replacement takes the services, the guard and its timer in a single blow, and nothing is left to restore any of it; the pipeline then stays dead until a human notices the silence. Design against that from the start: (1) keep a persistent copy of every file you install into system directories, outside those directories, plus a short written manifest of what was installed; (2) schedule the guard from somewhere that survives the host being replaced whenever such a scheduler exists — the agent platform's own scheduler, a supervisor outside the VM, any clock that is not stored in the wiped directories — and treat the systemd timer as the fallback for hosts that persist, not as the mechanism of record on ephemeral ones; (3) after any host replacement, check the guard's own survival before checking the pipeline.

- **One guardian per deployment.** Before installing this guard, look for a recovery mechanism the deployment already has — a platform watchdog, a manifest-driven guardian, an existing scheduler entry that restores system files. If one exists, register these units and their persistent copies with it instead of installing a second guardian; two guardians restoring the same services will fight, and the second one usually hides the first one's alerts.

- Only an `ALERT` wakes the agent: once per distinct alert, and at most once an hour while the same alert persists, through a configurable command (`ASR_GUARD_ALERT_CMD`) pointed at whatever reaches the agent. When an alert does reach you, work the Troubleshooting section below — the guard has already tried the restarts.

## Transcript gate (a pusher, not the agent's memory)

- Do not rely on the agent remembering to check the transcripts file before answering. Under conversational pressure the agent *will* answer from the platform's garbled transcript — this failure recurred until it was mechanized away. Deliver the transcript to the agent instead: a tiny local script (`scripts/voice_gate.py`) compares the landed audio files, the transcripts file, and a delivered-set state file, and a fast hook or cron (every ~5 seconds) runs it in `deliver` mode so each new transcript line is pushed to the agent's attention within seconds, exactly once. The first run baselines silently — it marks everything already transcribed as delivered and never floods history.

- Cadence is the mechanism's working part: a 60-second push interval was too slow (replies were composed before the transcript arrived), 5 seconds holds. The gate also watches the reverse gap: an audio file older than a grace window (180 s in the reference script) with no transcript raises a pipeline alert instead of another silence.

- A trailing transcripts line without its newline may be mid-append by the watcher; the gate never treats a partial line as a complete transcript.

- The gate guarantees the agent *has* the accurate transcript in hand. What the agent must then do with the wait is the first rule of the Reply flow below.

## Reply flow (the core behavior)

- **Hold the turn.** When a voice note arrives and its local transcript is not in hand yet, do not end the turn — no empty completion, and no "one moment" placeholder either. Poll the transcripts file *inside the turn* (bounded: about 90 seconds at the outside), then answer substantively in that same turn. Why this is a hard rule: a turn-based voice terminal consumes whatever completes the turn as the round's answer. An empty or placeholder completion makes the device play its "didn't hear you" fallback and close the round — and the substantive reply, sent afterwards as a new turn, can no longer be delivered into the closed round. The user hears "didn't hear you" for a perfectly audible note, then gets the real answer one round late, attached to the next note, or never. This presents exactly like an ASR or network failure and is neither.

- This hold rule governs waiting *to understand* (seconds, transcript in flight). It does not repeal the headline rule below, which governs waiting *for work* (you know the request; the work is slow): headline immediately, full result on the channel the user reads. Know which wait you are in.

- When the transcript is in hand, read the platform auto-transcript that came with the message, compare, and proceed as below.

- When the two transcripts disagree, trust the local one. The platform transcript is known to produce garbled text and wrong-language output on short or quiet recordings.

- Answer the user directly from the agreed meaning. Do not open your reply with a recap of the transcript; show the transcript only when the user asks, or when you must correct a misunderstanding — then state both what you first heard and what the local transcript says.

- If the request needs slow work (fetching account or market data, running a task, searching), immediately send one short, complete sentence to the board saying the headline or what is under way, so the board's turn ends at once. Never make the board wait silently for the finished result.

- Deliver the full result afterwards on the channel the user actually reads, and record that choice in memory at setup time. Default when the user has no board history page and mainly uses the board: send the full result to the board as a follow-up message when it is ready, kept as short as the content allows. If the user reads the main chat app, or their board has a history page that keeps past replies, deliver the full result to the main chat history instead and let the board message stay a one-line summary.

- Keep board replies plain text only, short, conclusion first, and merged into a single message. The board's screen renders no Markdown, and the user may be reading your replies as notifications on smart glasses, so multiple fragments are a disturbance.

## Safety

- Treat board voice as an unauthenticated channel: the device has no speaker verification, and other people (family, colleagues) may pick it up and talk. The voice you hear is not proof of who is speaking.

- When a board voice request is odd or sensitive — asking for account passwords or other credentials, moving money, changing system configuration — do not comply on voice alone. Confirm with the user in the typed main chat first.

- Never recite passwords, keys, or tokens in chat, regardless of which channel asks. Credentials go through the secure vault flow only.

## Troubleshooting

- If new lines in the transcripts file read `[FAILED]`, check the ASR server first: `curl http://127.0.0.1:18765/health`. If it does not answer, restart it with `systemctl restart asr-server` and confirm the watcher (`asr-watch`) is active too.

- If the board keeps answering "didn't hear you", or the user reports answers arriving one round behind, while transcripts are landing fine — check the agent's own turn behavior before the pipeline: an empty or placeholder turn sent while waiting for the transcript closes the device's round (see the hold rule under Reply flow). Neither the ASR nor the network is involved.

- If `/health` on the dual-engine server shows the `fallback_*` counters climbing as a share of requests, transcripts are still correct (the fallback is carrying them), but the primary is struggling on the current traffic — changed accents, clip lengths, or a bad model file. Investigate at leisure; treat a sudden climb as an early warning, not an outage.

- If the transcripts file stops growing while voice notes keep arriving, check the watcher log next to the script (`asr_watch.log`) and confirm the watcher service is active.

- Remember the timestamps in the transcripts file are the VM's system time, which may be UTC while the user is in another timezone. Note ids, not timestamps, are the reliable ordering.

- If everything is gone at once — both units, the guard and its schedule — the host was most likely replaced, and the guard had been scheduled only from inside the wiped directories. Restore the units from the persistent copies, re-establish the guard's schedule first, and move that schedule somewhere that survives the host (see Health guard) before calling it fixed, or the same silence repeats at the next replacement.
