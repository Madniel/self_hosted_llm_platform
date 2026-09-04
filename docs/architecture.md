# Architecture

How the serving path is put together, and why. Deployment lives in
[deployment.md](deployment.md); sizing the bounds lives in [tuning.md](tuning.md).

## Why admission control

An LLM engine has a fixed KV-cache and compute budget. Hand it every arriving request
and you get the classic unbounded-queue collapse: everyone's latency degrades together,
clients time out on work the server is still paying for, and effective throughput falls.

The policy here is deliberately boring and predictable:

```
arrival ──► at capacity? ──no──► run immediately
                │yes
                ▼
          queue full? ──yes──► 429 + Retry-After      (shed early, shed cheap)
                │no
                ▼
        FIFO wait ≤ queue_timeout ──expired──► 503 + Retry-After
                │
                ▼
          run, stream, release slot
```

Three properties are load-bearing:

1. **Reject before the response starts.** Admission happens *before* the `StreamingResponse`
   is constructed, so overload is a real HTTP status — not an error buried inside a 200
   stream that clients have to parse to discover.
2. **Rejection is O(µs).** A client that gets a 429 in 2 ms can retry elsewhere. One that
   gets a 30 s timeout has burned capacity for nothing. (Tested: `test_rejection_is_fast`.)
3. **The queue is FIFO.** `asyncio.Semaphore` gives no ordering guarantee, which lets late
   arrivals barge past requests that have already been waiting — unbounded tail latency
   under sustained load. `FairSemaphore` hands permits out strictly in arrival order and
   never loses one when a waiter is cancelled or times out.

---

## API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/completions` | OpenAI text completions, `stream: true` for SSE |
| `POST` | `/v1/chat/completions` | OpenAI chat completions, `stream: true` for SSE |
| `GET` | `/v1/models` | served model card |
| `GET` | `/healthz` | liveness |
| `GET` | `/readyz` | readiness — 503 while loading or draining |
| `GET` | `/stats` | admission-control counters, human-readable |
| `GET` | `/metrics` | Prometheus exposition |
| `POST` | `/admin/drain` | pre-stop hook: stop accepting, finish in-flight work |

Errors use the OpenAI error envelope, with `Retry-After` on 429/503:

```json
{"error": {"message": "server at capacity: 8 running, 32 queued (max 32)",
           "type": "rate_limit_error", "code": "queue_full", "param": null}}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | bad body, empty prompt, prompt over the character limit |
| 401 | `invalid_api_key` | auth enabled and the key did not match |
| 404 | `model_not_found` | asked for a model this server does not serve |
| 429 | `queue_full` | at capacity **and** the queue is full — retry elsewhere |
| 503 | `queue_timeout` | waited longer than `queue_timeout_s` for a slot |
| 503 | `shutting_down` | draining |
| 504 | `request_timeout` | generation exceeded `request_timeout_s` |

### Streaming details

* SSE frames are `data: {...}\n\n`, terminated by `data: [DONE]`.
* Deltas are incremental — the vLLM adapter diffs vLLM's cumulative outputs so the wire
  only ever carries new text.
* `X-Accel-Buffering: no` and `Cache-Control: no-store` keep nginx/envoy from buffering
  the stream into uselessness.
* A keep-alive comment (`: keep-alive`) is emitted during silent gaps, so long prefills
  and queue waits don't get the connection culled by an idle proxy.
* **Client disconnect frees the slot.** Starlette closes the generator, which aborts the
  engine request and releases the lease in a `finally` — no paying for tokens nobody
  will read. (Tested: `test_streaming_slot_is_released_when_client_disconnects`.)

---

## Observability

`/metrics` exposes, with buckets chosen for interactive serving:

| Metric | Type | What it answers |
|---|---|---|
| `llmserve_ttft_seconds` | histogram | how long until the user sees text (queue wait included) |
| `llmserve_inter_token_seconds` | histogram | how fast the text scrolls |
| `llmserve_queue_wait_seconds` | histogram | how much of TTFT is queueing vs. prefill |
| `llmserve_request_duration_seconds` | histogram | end-to-end, by outcome |
| `llmserve_requests_total` | counter | by route and outcome (`length`/`stop`/`cancelled`/`error`/`timeout`) |
| `llmserve_rejections_total` | counter | by reason (`queue_full`/`queue_timeout`/`shutting_down`) |
| `llmserve_in_flight_requests`, `llmserve_queue_depth` | gauge | live saturation |
| `llmserve_max_concurrency` | gauge | the configured bound, for headroom alerts |
| `llmserve_generated_tokens_total`, `llmserve_prompt_tokens_total` | counter | token throughput |

TTFT is measured from **request arrival**, not from admission, so queueing shows up in
the number the user actually feels.

Logs are structured JSON with a per-request correlation id (`X-Request-Id`, honoured from
the inbound header if present), one line per completed request:

```
{"level":"INFO","msg":"request complete","request_id":"req_0e2a...","route":"chat",
 "outcome":"length","queue_wait_ms":5774.36,"ttft_ms":6011.8,"duration_ms":7040.28,
 "prompt_tokens":108,"completion_tokens":48,"output_tps":45.7}
```

---

## Configuration

Every setting is an `LLMSERVE_`-prefixed environment variable (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `LLMSERVE_BACKEND` | `mock` | `mock` or `vllm` |
| `LLMSERVE_MODEL` | `facebook/opt-125m` | HF model id or local path |
| `LLMSERVE_MAX_CONCURRENT_REQUESTS` | `8` | execution slots — the hard concurrency bound |
| `LLMSERVE_MAX_QUEUE_SIZE` | `64` | waiters allowed past the bound; beyond this → 429 |
| `LLMSERVE_QUEUE_TIMEOUT_S` | `15` | max wait for a slot before 503 |
| `LLMSERVE_REQUEST_TIMEOUT_S` | `600` | max generation wall time before 504 |
| `LLMSERVE_MAX_TOKENS_CAP` | `2048` | server-side clamp on `max_tokens` |
| `LLMSERVE_MAX_PROMPT_CHARS` | `64000` | reject oversized prompts before admission |
| `LLMSERVE_STREAM_KEEPALIVE_S` | `15` | SSE keep-alive interval (0 disables) |
| `LLMSERVE_DRAIN_TIMEOUT_S` | `30` | shutdown drain budget |
| `LLMSERVE_API_KEYS` | *(empty)* | comma-separated bearer tokens; empty disables auth |
| `LLMSERVE_LOG_JSON` | `true` | structured vs. human-readable logs |
| `LLMSERVE_MOCK_*` | | mock backend timing shape (`TTFT_MS`, `ITL_MS`, `JITTER`, `CONTENTION`) |
| `LLMSERVE_VLLM_*` | | passed to `AsyncEngineArgs` (TP size, GPU mem fraction, dtype, max model len) |

---

## Tests

```bash
make test     # 54 tests
```

Coverage is concentrated where the risk is — the concurrency primitives and the
overload path, not the happy path:

* **`test_admission.py`** — capacity, queue-full rejection, rejection latency, queue
  timeout, FIFO ordering, permit not leaked on cancellation or timeout, idempotent
  release, non-blocking pre-stop drain, drain timeout.
* **`test_engine.py`** — token counts, seeded determinism, stop sequences, abort mid-flight,
  contention slowdown, generator cleanup when the consumer walks away.
* **`test_streaming.py`** — SSE framing, keep-alive during silent gaps, source cleanup,
  TTFT/ITL accounting.
* **`test_api.py`** — streaming and blocking on both routes, `max_tokens` clamping,
  OpenAI error shapes, 429/503/504 paths, slot release after every outcome including
  client disconnect, auth, metrics, drain semantics.
* **`test_loadtest.py`** — the harness's own statistics, and two live runs against the
  real ASGI app (one normal, one deliberately overloaded).

## Known limitations

* **Chat templating is generic.** `build_prompt` renders `Role: content` lines; a real
  deployment should use `tokenizer.apply_chat_template` so special tokens match training.
* **Token counts on the mock backend are estimates** (`len/4`); vLLM reports real ones.
* **Admission control is per-process.** Multi-replica deployments need the bound enforced
  per replica plus a load balancer that respects 429 — or a shared limiter.
* **No priority tiers.** One FIFO queue for everyone; separate queues (or a deficit
  scheduler) would be the next step for mixed interactive/batch traffic.
* **No prefix-cache awareness.** Routing requests with shared prefixes to the same replica
  would raise cache hit rate meaningfully.
