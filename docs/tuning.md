# Tuning and runbook

## Sizing the two bounds

`max_concurrent_requests` and `max_queue_size` are the only two numbers that decide how
this server behaves under overload. Set them from measurements, not vibes.

**1. Find the concurrency knee.** Run a closed-loop sweep and look for where `gput/s`
stops rising:

```bash
python -m loadtest --concurrency 1,2,4,8,16,32,64 --duration 30 --max-tokens 128
```

Throughput climbs while the engine has spare batch capacity, then flattens. Past the
knee, extra concurrency only adds queue wait — and on a real GPU it also risks KV-cache
preemption, where vLLM evicts and recomputes sequences. Set `max_concurrent_requests` at
or just below the knee.

**2. Derive the queue from your TTFT budget.**

```
max_queue_size ≈ max_concurrent_requests × (acceptable_wait / mean_service_time)
queue_timeout_s ≈ acceptable_wait
```

8 slots, ~1.4 s mean service time, a 2 s TTFT promise → queue ≈ 11, timeout ≈ 2 s. A
deeper queue does not add throughput; it converts fast rejections into slow ones. The
sweep in the README shows exactly that failure — a queue of 32 pushed TTFT p50 to 4.5 s
while goodput stayed flat.

**3. Verify with open loop.** Closed-loop load politely backs off when the server slows
down, so it cannot show you a shedding failure:

```bash
python -m loadtest --rps 5,10,20,40,80 --duration 30 --json results/open.json
```

What you want to see past saturation: `gput/s` flat, `shed` rising, `err` at zero.
`err > 0` means clients hit socket timeouts instead of getting a 429 — the queue is too
deep or `queue_timeout_s` is too generous.

## Alerts worth having

| Signal | Expression (sketch) | Why |
|---|---|---|
| Saturation | `llmserve_in_flight_requests / llmserve_max_concurrency > 0.9` for 5m | no headroom left; scale out |
| Shedding | `rate(llmserve_rejections_total[5m]) > 0` | capacity is short, or a client is misbehaving |
| Queue-dominated TTFT | `queue_wait p95 / ttft p95 > 0.5` | latency is queueing, not compute — the queue is too deep |
| Decode regression | `inter_token_seconds p95` up with flat in-flight | engine-side problem (preemption, thermal, a bad model swap) |
| Client abandonment | `rate(llmserve_requests_total{outcome="client_disconnect"}[5m])` | users are giving up before the answer lands |

## Interpreting the outcome label

`llmserve_requests_total` is labelled by outcome:

* `length` / `stop` — normal completion.
* `client_disconnect` — client hung up mid-stream; the slot was freed and the engine
  request aborted. A rising rate usually means TTFT is past what users will wait for.
* `cancelled` — the server-side task was cancelled (shutdown, or the ASGI server closed
  the connection).
* `timeout` — hit `request_timeout_s`. Either the cap is too tight or `max_tokens_cap`
  is too loose.
* `error` — the engine raised. Should be zero; page on it.

## Common situations

**Shedding at low utilisation.** `in_flight` well under the bound but 429s appearing:
requests are arriving in bursts faster than they drain. Raise `max_queue_size` a little,
or smooth arrivals client-side. Do not raise `max_concurrent_requests` — the engine is
not the constraint.

**TTFT fine, ITL bad.** Concurrency is too high for the engine: the batch is large, so
each sequence gets a thinner slice of each decode step. Lower `max_concurrent_requests`.

**Everything slow after a model change.** Check `max_model_len` and
`gpu_memory_utilization` — a longer context or a tighter memory fraction shrinks the KV
cache, which shrinks the effective batch and triggers preemption.

**Drain never finishes.** `drain_timeout_s` must exceed the longest possible generation
(`request_timeout_s`, or `max_tokens_cap × mean ITL`), and Kubernetes'
`terminationGracePeriodSeconds` must exceed `drain_timeout_s`.
