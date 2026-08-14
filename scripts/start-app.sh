#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${TMPDIR:-/tmp}"
PID_FILE="$RUNTIME_DIR/kalshi-data-stats.pid"
LOG_FILE="$RUNTIME_DIR/kalshi-data-stats.log"
PORT="${KALSHI_PORT:-8000}"

LISTENER_PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [[ -n "$LISTENER_PID" ]]; then
  LISTENER_COMMAND="$(ps -p "$LISTENER_PID" -o command=)"
  if [[ "$LISTENER_COMMAND" == *"backend.app"* ]]; then
    echo "$LISTENER_PID" >"$PID_FILE"
    echo "Kalshi Data Stats is already running at http://127.0.0.1:$PORT (PID $LISTENER_PID)."
    exit 0
  fi
  echo "Port $PORT is already in use by another process (PID $LISTENER_PID)." >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  PID="$(<"$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "Kalshi Data Stats is already running at http://127.0.0.1:$PORT (PID $PID)."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$ROOT_DIR"
npm --prefix frontend run build

nohup env UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/kalshi-uv-cache}" \
  uv run python -m backend.app >"$LOG_FILE" 2>&1 &
LAUNCH_PID=$!

for _ in {1..30}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/api/v1/health" >/dev/null; then
    PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -z "$PID" ]] || [[ "$(ps -p "$PID" -o command=)" != *"backend.app"* ]]; then
      echo "The app responded on port $PORT, but its process could not be identified safely." >&2
      exit 1
    fi
    echo "$PID" >"$PID_FILE"
    echo "Kalshi Data Stats started at http://127.0.0.1:$PORT (PID $PID)."
    exit 0
  fi
  if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
    echo "The app did not start. See $LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

echo "The app did not become ready in 30 seconds. See $LOG_FILE" >&2
exit 1
