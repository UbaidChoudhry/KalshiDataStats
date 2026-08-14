#!/usr/bin/env bash
set -euo pipefail

PID_FILE="${TMPDIR:-/tmp}/kalshi-data-stats.pid"
PORT="${KALSHI_PORT:-8000}"

PID=""
if [[ -f "$PID_FILE" ]]; then
  SAVED_PID="$(<"$PID_FILE")"
  if kill -0 "$SAVED_PID" 2>/dev/null && [[ "$(ps -p "$SAVED_PID" -o command=)" == *"backend.app"* ]]; then
    PID="$SAVED_PID"
  else
    rm -f "$PID_FILE"
  fi
fi

# A direct foreground launch is still the same local app. Fall back to the
# bound loopback listener so Stop remains useful after a stale PID file.
if [[ -z "$PID" ]]; then
  LISTENER_PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [[ -n "$LISTENER_PID" ]] && [[ "$(ps -p "$LISTENER_PID" -o command=)" == *"backend.app"* ]]; then
    PID="$LISTENER_PID"
  fi
fi

if [[ -z "$PID" ]]; then
  echo "Kalshi Data Stats is already stopped."
  exit 0
fi

kill -TERM "$PID"
for _ in {1..10}; do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Kalshi Data Stats stopped."
    exit 0
  fi
  sleep 1
done

echo "The app is still stopping (PID $PID). Run this script again in a few seconds." >&2
exit 1
