#!/usr/bin/env bash
# Drives the 90-second demo one beat at a time, pausing between so you can record.
# Usage:  ./demo/run_demo.sh [beat]     # beat = 1|2|3, omit for all
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BEAT="${1:-0}"
PORT="${PORT:-8000}"
CONCURRENCY="${CONCURRENCY:-8}"
# 16, not the shipped default of 64: at ~4 req/s drain a 64-deep queue is a ~15s
# wait, and the demo spends its budget watching a progress bar. Shed behaviour is
# identical; only the waiting changes. See demo/README.md.
QUEUE_SIZE="${QUEUE_SIZE:-16}"
BASE="http://127.0.0.1:${PORT}"
PYTHON="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

server=""
cleanup() {
  if [ -n "$server" ] && kill -0 "$server" 2>/dev/null; then
    printf '\n\033[36mstopping server (graceful drain)\033[0m\n'
    kill -TERM "$server" 2>/dev/null || true
    wait "$server" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

beat() {
  printf '\n\033[90m%s\033[0m\n  %s\n\033[90m%s\033[0m\n' \
    "------------------------------------------------------------------------" \
    "$1" \
    "------------------------------------------------------------------------"
  [ "${NOPAUSE:-}" = "1" ] || read -r -p "  press Enter when you are recording" _
}

printf '\n\033[36mstarting server: %s concurrent, queue %s, mock backend\033[0m\n' \
  "$CONCURRENCY" "$QUEUE_SIZE"
LLMSERVE_BACKEND=mock \
LLMSERVE_MAX_CONCURRENT_REQUESTS="$CONCURRENCY" \
LLMSERVE_MAX_QUEUE_SIZE="$QUEUE_SIZE" \
LLMSERVE_LOG_JSON=false \
LLMSERVE_PORT="$PORT" \
  "$PYTHON" -m llmserve &
server=$!

for _ in $(seq 1 40); do
  sleep 0.25
  if curl -sf "$BASE/readyz" >/dev/null 2>&1; then printf '  \033[32mready\033[0m\n'; break; fi
done

if [ "$BEAT" = 0 ] || [ "$BEAT" = 1 ]; then
  beat "BEAT 1  (0:15-0:30)  one request, tokens streaming back"
  "$PYTHON" scripts/stream_client.py --base-url "$BASE" --max-tokens 48
fi

if [ "$BEAT" = 0 ] || [ "$BEAT" = 2 ]; then
  beat "BEAT 2  (0:30-1:00)  ramp past capacity -- 200s continue, excess gets 429"
  "$PYTHON" -m demo.live_traffic --base-url "$BASE" --ramp 4,8,16,32 --hold 7
fi

if [ "$BEAT" = 0 ] || [ "$BEAT" = 3 ]; then
  beat "BEAT 3  (1:00-1:20)  sweep, then the chart"
  # In-process rather than `python -m loadtest`: the HTTP harness currently
  # mis-measures under overload (drifting arrivals, and a wall clock that
  # includes the post-arrival drain). Same controller and engine either way.
  "$PYTHON" -m demo.measure_inprocess --rps 2,4,6,8,12,16,24,32 \
      --duration 16 --warmup 4 --max-concurrent "$CONCURRENCY" \
      --max-queue-size "$QUEUE_SIZE" --json demo/out/sweep.json
  "$PYTHON" -m demo.chart demo/out/sweep.json -o demo/out/chart.html
  printf '\n\033[36mchart: %s\033[0m\n' "$PWD/demo/out/chart.html"
  (command -v xdg-open >/dev/null && xdg-open demo/out/chart.html) 2>/dev/null || true
fi

printf '\n\033[36mdone. Beat 4 is demo/architecture.svg and the takeaway.\033[0m\n'
