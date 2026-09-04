#!/usr/bin/env bash
# End-to-end demo: boot the server, stream a completion, then overload it on purpose.
set -euo pipefail

URL="${URL:-http://127.0.0.1:8000}"
PY="${PY:-.venv/bin/python}"

echo "== starting server (1 slot, no queue, so overload is easy to see) =="
LLMSERVE_BACKEND=mock \
LLMSERVE_MAX_CONCURRENT_REQUESTS=2 \
LLMSERVE_MAX_QUEUE_SIZE=4 \
LLMSERVE_QUEUE_TIMEOUT_S=5 \
LLMSERVE_LOG_JSON=false \
"$PY" -m llmserve &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

until curl -sf "$URL/readyz" >/dev/null; do sleep 0.2; done
echo "ready."

echo
echo "== streaming a chat completion =="
curl -N -s "$URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"explain paged attention"}],"max_tokens":24,"stream":true}' \
  | head -n 12

echo
echo "== open-loop overload: watch shed rate and TTFT =="
"$PY" -m loadtest --base-url "$URL" --rps 5,20,60 --duration 8 --max-tokens 48

echo
echo "== admission control counters =="
curl -s "$URL/stats" | python3 -m json.tool
