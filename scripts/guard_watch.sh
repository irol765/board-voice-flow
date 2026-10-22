#!/bin/bash
# guard_watch.sh - zero-token poller around asr_guard.sh.
#
# Runs the guard, appends every status line to a state log, and stays
# silent while the line is OK or RECOVERED (a self-recovery is not
# news). Only an ALERT - the guard could not fix it - escalates, by
# running $ASR_GUARD_ALERT_CMD with the line in $ASR_GUARD_LINE:
# once per distinct alert, and at most once an hour while the same
# alert persists. Point ASR_GUARD_ALERT_CMD at whatever wakes your
# agent (a chat ping, a webhook, a CLI) - no LLM runs in this loop.
#
# Driven by systemd/asr-guard.timer (every 5 minutes).
set -u

GUARD="${ASR_GUARD_SCRIPT:-$HOME/workspace/tools/asr_guard.sh}"
STATE_DIR="${ASR_GUARD_STATE:-$HOME/.local/state/asr-guard}"
LOG="$STATE_DIR/guard.log"
SIGF="$STATE_DIR/alert.state"
ALERT_CMD="${ASR_GUARD_ALERT_CMD:-}"
mkdir -p "$STATE_DIR"

OUT=$("$GUARD" 2>/dev/null || echo "ALERT asr-guard guard-exec-failed")
echo "$(date '+%F %T') $OUT" >> "$LOG"
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

case "$OUT" in
  ALERT*)
    SIG=$(printf '%s' "$OUT" | md5sum | cut -d' ' -f1)
    LASTSIG=""; LASTWAKE=0
    if [ -f "$SIGF" ]; then read -r LASTSIG LASTWAKE < "$SIGF" || true; fi
    NOW=$(date +%s)
    if [ "$SIG" != "$LASTSIG" ] || [ $((NOW - LASTWAKE)) -ge 3600 ]; then
      echo "$SIG $NOW" > "$SIGF"
      if [ -n "$ALERT_CMD" ]; then
        ASR_GUARD_LINE="$OUT" bash -c "$ALERT_CMD" || true
      else
        logger -t asr-guard "$OUT" 2>/dev/null || true
        echo "$OUT" > "$STATE_DIR/ALERT"
      fi
    fi
    ;;
  *)
    rm -f "$SIGF" "$STATE_DIR/ALERT"
    ;;
esac
exit 0
