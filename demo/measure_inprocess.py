"""Measure admission-control behaviour without a server, a GPU, or the network.

This drives the *real* :class:`AdmissionController` and :class:`MockEngine` from the
package, with the same open-loop Poisson arrival process the HTTP harness uses. It
exists so the demo chart can be produced (and regression-checked) anywhere Python
runs -- CI included -- rather than only on a machine that can serve HTTP.

What it does NOT cover: the HTTP layer. Status codes here are the ones the routes
*would* map each admission outcome onto, not codes observed on the wire. For the
recorded demo, drive the real server with ``python -m loadtest`` instead; this
module is for reproducing the numbers.

    python -m demo.measure_inprocess --rps 2,4,6,8,12,16,24,32 --json demo/sample/sweep.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from llmserve.admission import AdmissionController
from llmserve.engine.base import GenerationRequest, SamplingConfig
from llmserve.engine.mock import MockEngine
from llmserve.errors import AdmissionError, QueueFullError, QueueTimeoutError

# The route layer maps each admission failure onto one of these.
STATUS_FOR = {QueueFullError: 429, QueueTimeoutError: 503}

_WORDS = (
    "bounded concurrency keeps the engine busy without letting the queue grow without "
    "limit which is what turns an overload into a fast refusal instead of a timeout"
).split()


@dataclass
class Outcome:
    ok: bool
    status: int
    e2e_s: float
    ttft_s: float | None = None
    tokens: int = 0
    itls: list[float] = field(default_factory=list)
    reject_reason: str | None = None
    completed_at: float = 0.0
    """Seconds since the phase began -- used to score only the steady-state window."""


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile; matches the harness's definition."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (pct / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def summarise(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


async def one_request(
    admission: AdmissionController,
    engine: MockEngine,
    req_id: str,
    prompt: str,
    max_tokens: int,
    origin: float,
) -> Outcome:
    """Admit (or shed), then stream, timing exactly as the server would."""
    arrived = time.perf_counter()
    try:
        lease = await admission.acquire(req_id)
    except AdmissionError as exc:
        # Shed before a response is started -- the whole point of the design.
        return Outcome(
            ok=False,
            status=STATUS_FOR.get(type(exc), 503),
            e2e_s=time.perf_counter() - arrived,
            reject_reason=getattr(exc, "code", type(exc).__name__),
            completed_at=time.perf_counter() - origin,
        )

    ttft: float | None = None
    last: float | None = None
    itls: list[float] = []
    tokens = 0
    try:
        request = GenerationRequest(
            request_id=req_id,
            prompt=prompt,
            sampling=SamplingConfig(max_tokens=max_tokens),
        )
        async for chunk in engine.generate(request):
            if chunk.is_final:
                break
            now = time.perf_counter()
            if ttft is None:
                # Measured from ARRIVAL, so queue wait is inside TTFT. A client
                # cannot tell queueing from slow prefill, so neither should this.
                ttft = now - arrived
            elif last is not None:
                itls.append(now - last)
            last = now
            tokens += 1
    finally:
        lease.release()

    return Outcome(
        ok=True,
        status=200,
        e2e_s=time.perf_counter() - arrived,
        ttft_s=ttft,
        tokens=tokens,
        itls=itls,
        completed_at=time.perf_counter() - origin,
    )


async def run_phase(
    rps: float,
    *,
    duration_s: float,
    warmup_s: float,
    max_concurrent: int,
    max_queue_size: int,
    queue_timeout_s: float,
    max_tokens: int,
    prompt_words: int,
    seed: int,
) -> dict:
    rng = random.Random(seed)
    engine = MockEngine(seed=seed)
    await engine.startup()
    admission = AdmissionController(
        max_concurrent=max_concurrent,
        max_queue_size=max_queue_size,
        queue_timeout_s=queue_timeout_s,
    )

    prompt = " ".join(rng.choice(_WORDS) for _ in range(prompt_words))
    tasks: list[asyncio.Task] = []
    started = time.perf_counter()
    issued = 0

    # Open loop: arrivals follow a Poisson process, so the offered rate is
    # independent of how fast the server is responding. A closed loop would
    # throttle itself against the server and never show the overload.
    #
    # Arrival times are absolute, not a chain of relative sleeps. Sleeping for
    # the gap each time accumulates the scheduling overshoot of every previous
    # iteration, and under a loaded event loop that compounds badly -- a 32 rps
    # target degenerates to about 12. Holding the schedule means a late wake-up
    # is caught up on rather than carried forward.
    next_at = 0.0
    arrivals: list[float] = []
    while True:
        next_at += rng.expovariate(rps)
        if next_at >= duration_s:
            break
        delay = next_at - (time.perf_counter() - started)
        if delay > 0:
            await asyncio.sleep(delay)
        issued += 1
        arrivals.append(next_at)
        tasks.append(
            asyncio.create_task(
                one_request(admission, engine, f"req-{issued}", prompt, max_tokens, started)
            )
        )

    # Arrivals have stopped. Anything still queued is tail, not steady state --
    # draining it would stretch the denominator (a 10s arrival window took ~26s
    # of wall clock at 32 rps, deflating every rate by 60%). Cancel the tail and
    # score only what resolved inside the measurement window.
    for t in tasks:
        if not t.done():
            t.cancel()
    settled = await asyncio.gather(*tasks, return_exceptions=True)
    await engine.shutdown()

    window = duration_s - warmup_s
    outcomes = [
        o
        for o in settled
        if isinstance(o, Outcome) and warmup_s <= o.completed_at <= duration_s
    ]
    in_flight_at_close = sum(1 for o in settled if not isinstance(o, Outcome))
    wall = window

    ok = [o for o in outcomes if o.ok]
    rejected = [o for o in outcomes if not o.ok and o.status in (429, 503)]
    failed = [o for o in outcomes if not o.ok and o.status not in (429, 503)]

    reasons: dict[str, int] = {}
    for o in rejected:
        reasons[o.reject_reason or "unknown"] = reasons.get(o.reject_reason or "unknown", 0) + 1
    codes: dict[str, int] = {}
    for o in outcomes:
        codes[str(o.status)] = codes.get(str(o.status), 0) + 1

    tokens = sum(o.tokens for o in ok)
    return {
        "label": f"{rps:g} rps",
        "concurrency": max_concurrent,
        "rps_target": rps,
        "wall_s": wall,
        "in_flight_at_close": in_flight_at_close,
        "arrivals_in_window": sum(1 for a in arrivals if warmup_s <= a <= duration_s),
        "issued": len(outcomes),
        "succeeded": len(ok),
        "rejected": len(rejected),
        "failed": len(failed),
        "throughput_rps": len(outcomes) / wall if wall else 0.0,
        "goodput_rps": len(ok) / wall if wall else 0.0,
        "output_tokens": tokens,
        "output_tps": tokens / wall if wall else 0.0,
        "ttft": summarise([o.ttft_s for o in ok if o.ttft_s is not None]),
        "itl": summarise([v for o in ok for v in o.itls]),
        "e2e": summarise([o.e2e_s for o in ok]),
        "reject_reasons": reasons,
        "status_codes": codes,
        "server_stats": admission.snapshot(),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="demo.measure_inprocess",
        description="Measure goodput and shed rate against the real admission controller.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rps", default="2,4,6,8,12,16,24,32", help="offered rates to sweep")
    p.add_argument("--duration", type=float, default=12.0, help="arrival window, seconds")
    p.add_argument("--warmup", type=float, default=3.0, help="leading seconds excluded from rates")
    p.add_argument("--max-concurrent", type=int, default=8)
    p.add_argument("--max-queue-size", type=int, default=64)
    p.add_argument("--queue-timeout", type=float, default=15.0)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--prompt-words", type=int, default=60)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--json", dest="json_out", default=None)
    return p


async def _main(args: argparse.Namespace) -> None:
    levels = [float(x) for x in args.rps.split(",") if x.strip()]
    phases = []
    header = f"{'offered':>9} {'goodput':>9} {'shed':>7} {'429':>6} {'503':>6} {'ttft p50':>9} {'ttft p95':>9}"
    print(header)
    print("-" * len(header))
    for rps in levels:
        phase = await run_phase(
            rps,
            duration_s=args.duration,
            warmup_s=args.warmup,
            max_concurrent=args.max_concurrent,
            max_queue_size=args.max_queue_size,
            queue_timeout_s=args.queue_timeout,
            max_tokens=args.max_tokens,
            prompt_words=args.prompt_words,
            seed=args.seed,
        )
        phases.append(phase)
        shed = phase["rejected"] / phase["issued"] if phase["issued"] else 0.0
        print(
            f"{rps:>8.0f}  {phase['goodput_rps']:>8.2f} {shed * 100:>6.0f}% "
            f"{phase['status_codes'].get('429', 0):>6} {phase['status_codes'].get('503', 0):>6} "
            f"{phase['ttft'].get('p50', 0) * 1000:>8.0f}ms {phase['ttft'].get('p95', 0) * 1000:>8.0f}ms"
        )

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "config": {
                        "source": "in-process (no HTTP layer)",
                        "max_concurrent": args.max_concurrent,
                        "max_queue_size": args.max_queue_size,
                        "queue_timeout_s": args.queue_timeout,
                        "max_tokens": args.max_tokens,
                        "prompt_words": args.prompt_words,
                        "duration_s": args.duration,
                        "warmup_s": args.warmup,
                        "seed": args.seed,
                    },
                    "phases": phases,
                },
                indent=2,
            )
        )
        print(f"\nwrote {path}")


def main() -> None:
    asyncio.run(_main(build_parser().parse_args()))


if __name__ == "__main__":
    main()
