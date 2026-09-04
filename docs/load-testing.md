# Load testing

The harness, the two load models, and measured results. Use
[tuning.md](tuning.md) to turn these numbers into settings.


```bash
# closed loop: N virtual users, each issuing the next request as the previous finishes
python -m loadtest --concurrency 1,2,4,8,16,32 --duration 15 --max-tokens 64

# open loop: Poisson arrivals that do NOT back off when the server slows down
python -m loadtest --rps 5,10,20,40 --duration 15 --json results/open.json
```

The open-loop model is the one that proves admission control works, because the load
generator keeps arriving at the target rate regardless of how the server is coping.

### Measured: closed-loop sweep

`mock` backend, 8 slots, queue 32, 48 tokens/request, 6 s per level:

```
     level   sent     ok   shed   err   gput/s    tok/s  ttft p50  ttft p95  itl p50   e2e p95
----------------------------------------------------------------------------------------------
       c=1      6      6      0     0      0.9       46       140       151       20      1080
       c=2     12     12      0     0      1.8       86       158       175       20      1146
       c=4     20     20      0     0      3.3      156       156       200       22      1272
       c=8     40     40      0     0      5.5      265       191       238       26      1478
      c=16     48     48      0     0      5.5      263      1635      1727       27      2982
      c=32     64     64      0     0      5.4      261      4504      4646       26      5901
```

Throughput saturates at the concurrency bound (~5.5 req/s at 8 slots) and stays flat.
Past saturation, extra load buys nothing and shows up entirely as queue wait: TTFT p50
goes 191 ms → 1.6 s → 4.5 s while goodput does not move. Inter-token latency stays ~26 ms
throughout — once a request is *running* it streams at full speed, which is the point of
bounding concurrency instead of letting the batch grow without limit.

### Measured: open-loop sweep (admission control shedding)

Same server, Poisson arrivals:

```
     level   sent     ok   shed   err   gput/s    tok/s  ttft p50  ttft p95  itl p50   e2e p95
----------------------------------------------------------------------------------------------
     rps=2     13     13      0     0      1.9       89       159       197       21      1284
     rps=5     39     39      0     0      4.6      223       636      1727       26      2992
    rps=10     65     65      0     0      5.2      248      3297      5220       26      6408
    rps=20    126     68     58     0      5.2      252      4735      5975       26      7228
    rps=40    236     69    167     0      5.3      256      5281      6009       26      7263
```

At 4× and 8× the sustainable rate the server sheds 46% and 71% of arrivals — and
**`err` stays 0**. Goodput holds at ~5.2 req/s instead of collapsing, and every rejected
client got a fast 429 with `Retry-After` rather than a hung socket. That is the whole
design goal in one table.

**Tuning note:** those TTFT numbers also show a queue that is too deep for this service
time. Size it from the wait you're willing to promise:
`max_queue_size ≈ max_concurrent × (acceptable_wait / mean_service_time)`. With 8 slots,
~1.4 s requests and a 2 s TTFT budget, the right queue is ~10, not 32 — and
`queue_timeout_s` should be set to that same budget so late waiters are shed instead of
served slowly.

---
