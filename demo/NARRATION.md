# 90-second demo — shot list and narration

Run `.\demo\run_demo.ps1` (or `./demo/run_demo.sh`). It pauses before each beat so
you can start recording and get a clean take.

Word counts are sized for ~150 wpm. Every number below is measured, not asserted —
`demo/sample/chart.html` is the run they come from.

---

## 0:00–0:15 · The shape of the thing

**Show:** `demo/architecture.svg`, full screen.

> A request arrives. Before anything expensive happens, admission control decides
> whether the server can actually take it. If a slot is free, it goes to the
> inference engine and tokens start streaming back. If all eight slots are busy,
> it waits briefly in a bounded queue. And if that queue is full, it's refused
> immediately — a 429, in about two milliseconds.

**Cue:** let the diagram sit for a beat on the orange path. That branch is the whole demo.

---

## 0:15–0:30 · One request, working

**Show:** beat 1 — a single streaming request, tokens appearing as they're produced.

> Normal case first. One request, and the response streams token by token — the
> first token arrives long before the last one is generated, which is what keeps
> this usable at a chat prompt.

**Cue:** don't talk over the stream. Let two or three seconds of tokens land silently.

---

## 0:30–1:00 · Past capacity

**Show:** beat 2 — `demo.live_traffic`, ramping 4 → 8 → 16 → 32 rps against the real
server. One row per second: blue `#` for streamed 200s, orange `x` for 429s.

> Now I'll push past what the server can do. At four requests a second, everything
> is served. At eight, the queue starts filling. And here — [**wait for the first
> orange row**] — the queue is full, so the excess is refused straight away.
>
> Notice what *doesn't* happen. The successful requests keep completing at the same
> rate. Nothing times out, nothing fails. The server isn't degrading — it's
> declining work it can't do.

**Cue:** the emotional beat is the first orange row. Pause there. Then point at the
blue column staying steady while orange grows.

---

## 1:00–1:20 · The numbers

**Show:** beat 3 — `demo/out/chart.html`.

> Offered load runs from 2 up to 32 requests a second — sixteen times. Goodput is
> the flat blue line: four a second, and it does not move. Everything above that is
> the orange line, shed. Zero failures across the whole sweep.
>
> Latency is the second panel. Time-to-first-token rises, because queue wait is
> inside it — but it's bounded, because the queue is. And the third panel is the
> one I'd point at: inter-token latency is flat at twenty-six milliseconds. Whoever
> got in gets the same experience at 32 requests a second as at two. Bounding
> concurrency is what buys that — the engine is never oversubscribed.

**Cue:** three panels is a lot for twenty seconds. Scroll once, slowly, top to bottom.

---

## 1:20–1:30 · The takeaway

**Show:** back to the diagram, or the chart's top panel.

> Two separate bounds are doing two separate jobs. The bound on concurrency sets
> throughput — that's the flat line. The bound on the queue sets how long someone
> waits before hearing "no". Without them, that orange line would be a backlog
> instead: work piling up for clients who already gave up, and a queue that takes
> longer to drain than anyone is still waiting.
>
> Refusing fast is a feature. A client that gets a 429 in two milliseconds can retry
> somewhere else. One that gets a thirty-second timeout has burned capacity for
> nothing.

---

## Notes for the take

- **Queue depth is set to 16, not the shipped default of 64.** At a ~4 req/s drain
  rate, a 64-deep queue is a ~15-second wait — the demo would spend its whole budget
  watching a progress bar, and p95 time-to-first-token pins to the 15s queue timeout.
  Goodput and shed behaviour are identical either way; only the waiting changes.
  Worth deciding whether 64 is right as a shipped default.
- **The chart is measured in-process**, driving the real `AdmissionController` and
  `MockEngine` without the HTTP layer, because the HTTP harness currently
  mis-measures under overload (see `demo/README.md`). Beat 2 is a real server over
  real HTTP, so the 429s on screen are genuine wire responses.
- If a take runs long, cut beat 1 to five seconds. The overload beat is the demo;
  everything else is setup.
