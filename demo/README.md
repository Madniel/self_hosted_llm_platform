# demo/

Everything needed to record the 90-second demo. `NARRATION.md` is the shot list and
script; this file is what the pieces are and how to run them.

```powershell
.\demo\run_demo.ps1              # all beats, pausing before each
.\demo\run_demo.ps1 -Beat 2      # just the overload beat
```

```bash
./demo/run_demo.sh               # same, on Linux/macOS
```

| File | Beat | What it is |
|---|---|---|
| `architecture.svg` | 0:00–0:15 | Request path, theme-aware, opens in any browser |
| `../scripts/stream_client.py` | 0:15–0:30 | One streaming request |
| `live_traffic.py` | 0:30–1:00 | Open-loop load at a **real server**, rendered live, one row per second |
| `measure_inprocess.py` | 1:00–1:20 | The sweep the chart is built from |
| `chart.py` | 1:00–1:20 | Sweep JSON &rarr; one self-contained HTML file |
| `sample/` | — | A committed run, so the chart can be seen without running anything |

## Why the chart is measured in-process

`measure_inprocess.py` drives the real `AdmissionController`, `FairSemaphore` and
`MockEngine` with the same open-loop Poisson arrivals the HTTP harness uses — it
just skips the HTTP layer. Status codes are the ones the routes *would* map each
admission outcome onto, not codes seen on the wire. Beat 2 uses the real server, so
the 429s on screen during the demo are genuine.

It is measured this way because **`loadtest` currently mis-measures under overload**,
in two independent ways:

1. **`_open_loop` under-offers.** It chains relative sleeps —
   `create_task(...)` then `await asyncio.sleep(expovariate(rps))`. Each iteration
   inherits the scheduling overshoot of every previous one, and once the event loop
   is busy that compounds. Measured here: a 32 rps target delivered ~12 rps.
   Arrival times need to be absolute, so a late wake-up is caught up on rather than
   carried forward.

2. **`run_phase`'s window includes the drain.** `started` is taken before the
   arrivals and `wall` after `gather()` has waited for every straggler, so a
   16-second arrival window can be divided by ~26 seconds of wall clock. Every rate
   derived from it — `throughput_rps`, `goodput_rps`, `output_tps` — is deflated,
   by ~60% at the rates in this sweep. Rates need a steady-state window: score only
   what resolved inside it, and drop the tail.

Both are fixable in `loadtest/harness.py` without changing its interface;
`measure_inprocess.py` shows the corrected shape of each. Until then, treat
`loadtest`'s absolute rates under overload as a lower bound — the *relative* story
(goodput flat, excess shed, nothing failing) is unaffected, since both numerator and
denominator move together.

## Queue depth in the demo

The runner sets `LLMSERVE_MAX_QUEUE_SIZE=16` rather than the shipped default of 64.
At a ~4 req/s drain rate, a 64-deep queue is a ~15-second wait, which is also
`queue_timeout_s` — so p95 time-to-first-token pins to the timeout and the demo
spends its budget watching a progress bar.

Measured across queue depths at fixed concurrency (8 slots, 32 rps offered):

| `max_queue_size` | goodput | shed | TTFT p95 |
|---|---|---|---|
| 8 | 4.3 req/s | 82% | 2.0s |
| 16 | 4.3 req/s | 79% | 3.9s |
| 64 | 4.3 req/s | 64% | 15.0s |

Goodput is flat across all three. Queue depth buys no throughput at all — it only
decides how long a client waits before being refused. That is a latency knob, not a
capacity knob, and it is worth asking whether 64 is the right shipped default.

## Reproducing the committed sample

```bash
python -m demo.measure_inprocess --rps 2,4,6,8,12,16,24,32 \
    --duration 16 --warmup 4 --max-queue-size 16 --json demo/sample/sweep.json
python -m demo.chart demo/sample/sweep.json -o demo/sample/chart.html
```

Deterministic given `--seed` (default 7), apart from real scheduling jitter. No
third-party dependencies and no network — `chart.py` inlines the SVG, so the output
is one file that opens offline.
