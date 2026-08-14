#!/usr/bin/env bash
set -euo pipefail

PID_FILE="${TMPDIR:-/tmp}/kalshi-data-stats.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Kalshi Data Stats is not running through scripts/start-app.sh."
  exit 0
fi

PID="$(<"$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Kalshi Data Stats is already stopped."
  exit 0
fi

COMMAND="$(ps -p "$PID" -o command=)"
if [[ "$COMMAND" != *"backend.app"* ]]; then
  rm -f "$PID_FILE"
  echo "The saved PID does not belong to Kalshi Data Stats; it was not stopped." >&2
  exit 1
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
