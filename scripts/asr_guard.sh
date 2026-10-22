#!/bin/bash
# asr_guard.sh - health guard for the board voice transcription pipeline.
#
# One run checks, in order:
#   1. Both systemd units (asr-server, asr-watch): unit file present
#      (a vanished file - e.g. after a VM rebuild - is reinstalled from
#      the kept copies in $TOOLS_DIR/systemd/), enabled, and active.
#   2. ASR server health: GET $HEALTH_URL; on failure restart the
#      asr-server unit once and re-check.
#   3. Transcript freshness: if the newest voice note's id is ahead of
#      the transcripts file and the note is older than $FRESH_SECS,
#      restart the asr-watch unit.
# It recovers what it can and prints exactly ONE line:
#   OK ... | RECOVERED ... | ALERT ...
# (first word is the status, the rest is detail). The exit code is
# always 0 - the line is the contract. Run it from guard_watch.sh on a
# timer; never from an agent cron (see SKILL.md, "Health guard").
set -u

WORKSPACE="${ASR_GUARD_WORKSPACE:-$HOME/workspace}"
TOOLS_DIR="${ASR_GUARD_TOOLS:-$WORKSPACE/tools}"
FILES_DIR="${ASR_GUARD_FILES:-$WORKSPACE/user/files}"
TRANSCRIPTS="${ASR_GUARD_TRANSCRIPTS:-$FILES_DIR/board-transcripts.txt}"
HEALTH_URL="${ASR_GUARD_HEALTH_URL:-http://127.0.0.1:18765/health}"
FRESH_SECS="${ASR_GUARD_FRESH_SECS:-240}"
UNITS="asr-server asr-watch"

STATUS="OK"
DETAILS=""
ALERTS=""
mark_recovered() { if [ "$STATUS" = "OK" ]; then STATUS="RECOVERED"; fi; DETAILS="$DETAILS $1"; }
mark_alert() { ALERTS="$ALERTS $1"; }
SUDO="sudo -n"

# --- 1) units present, enabled, active --------------------------------
for name in $UNITS; do
  f="/etc/systemd/system/$name.service"
  if [ ! -f "$f" ]; then
    $SUDO cp "$TOOLS_DIR/systemd/$name.service" "$f" 2>/dev/null
    $SUDO systemctl daemon-reload 2>/dev/null
    $SUDO systemctl enable --now "$name.service" 2>/dev/null
    if [ -f "$f" ]; then mark_recovered "unit-$name-missing-restored"; else mark_alert "unit-$name-restore-failed"; fi
  fi
  if [ -f "$f" ]; then
    if ! systemctl is-enabled --quiet "$name.service" 2>/dev/null; then
      $SUDO systemctl enable "$name.service" 2>/dev/null
      mark_recovered "unit-$name-was-disabled-enabled"
    fi
    if ! systemctl is-active --quiet "$name.service" 2>/dev/null; then
      $SUDO systemctl start "$name.service" 2>/dev/null
      sleep 2
      if systemctl is-active --quiet "$name.service" 2>/dev/null; then
        mark_recovered "service-$name-was-down-started"
      else
        mark_alert "service-$name-not-active"
      fi
    fi
  fi
done

# --- 2) ASR server health ---------------------------------------------
H=$(curl -s -m 3 "$HEALTH_URL" 2>/dev/null)
if [ -z "$H" ]; then
  $SUDO systemctl restart asr-server.service 2>/dev/null
  sleep 6
  H=$(curl -s -m 3 "$HEALTH_URL" 2>/dev/null)
  if [ -n "$H" ]; then
    mark_recovered "asr-server-was-down-restarted"
  else
    echo "ALERT asr-guard$DETAILS$ALERTS asr-server-health-still-failing-after-restart"
    exit 0
  fi
fi

# --- 3) transcript freshness ------------------------------------------
MAXWAV=$(ls "$FILES_DIR"/voice_note-*.wav 2>/dev/null | sed -E 's/.*voice_note-([0-9]+)\.wav/\1/' | sort -n | tail -1)
MAXTR=$(grep -oP '^[0-9]+' "$TRANSCRIPTS" 2>/dev/null | sort -n | tail -1)
if [ -n "${MAXWAV:-}" ] && [ -n "${MAXTR:-}" ] && [ "$MAXWAV" -gt "$MAXTR" ]; then
  NEWEST=$(ls -t "$FILES_DIR"/voice_note-*.wav 2>/dev/null | head -1)
  AGE=$(( $(date +%s) - $(stat -c %Y "$NEWEST") ))
  if [ "$AGE" -gt "$FRESH_SECS" ]; then
    $SUDO systemctl restart asr-watch.service 2>/dev/null
    mark_recovered "stale-transcription wav=$MAXWAV transcribed=$MAXTR age=${AGE}s watch-restarted"
  fi
fi

# --- 4) unresolved [FAILED] retries ------------------------------------
# The watcher retries every [FAILED] entry once after health recovers;
# entries that still fail are listed in $FILES_DIR/asr-failed.flag (one
# id per line). A non-empty flag is an ALERT: a silent loss must surface.
FAILED_FLAG="$FILES_DIR/asr-failed.flag"
if [ -s "$FAILED_FLAG" ]; then
  mark_alert "asr-failed-unresolved:$(paste -sd, "$FAILED_FLAG" 2>/dev/null)"
fi

if [ -n "$ALERTS" ]; then STATUS="ALERT"; fi
echo "$STATUS asr-guard$DETAILS$ALERTS"
