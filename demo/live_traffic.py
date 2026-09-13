"""Drive open-loop traffic at a real server and render what happens, per second.

This is the 30-60s beat of the demo. The load harness reports a table when it
finishes; here the point is watching the split happen live -- successful requests
continuing at a steady rate while everything above capacity is refused.

    python -m demo.live_traffic --rps 4          # inside capacity
    python -m demo.live_traffic --rps 32         # well past it
    python -m demo.live_traffic --ramp 4,8,16,32 --hold 6

Each row is one wall-clock second:

    t+07  offered 32  ok ####  4   shed xxxxxxxxxxxxxxxxxxxxxxxxxxxx 28   p50 ttft 7.2s

Requires httpx (already in requirements-dev.txt). Run it against a live server;
for the numbers without a server, use demo.measure_inprocess instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field

import httpx

RESET, DIM, OK, SHED, BOLD = "\033[0m", "\033[2m", "\033[38;5;33m", "\033[38;5;166m", "\033[1m"


@dataclass
class Second:
    offered: int = 0
    ok: int = 0
    shed: int = 0
    failed: int = 0
    ttfts: list[float] = field(default_factory=list)


def bar(n: int, glyph: str, width: int = 34) -> str:
    return glyph * min(n, width) + ("+" if n > width else "")


async def one(client: httpx.AsyncClient, body: dict, buckets: dict, t0: float, color: bool) -> None:
    sec = int(time.perf_counter() - t0)
    buckets.setdefault(sec, Second()).offered += 1
    started = time.perf_counter()
    try:
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                await resp.aread()
                slot = buckets.setdefault(int(time.perf_counter() - t0), Second())
                if resp.status_code in (429, 503):
                    slot.shed += 1
                else:
                    slot.failed += 1
                return
            ttft = None
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                if ttft is None:
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if (obj.get("choices") or [{}])[0].get("delta", {}).get("content"):
                        ttft = time.perf_counter() - started
            slot = buckets.setdefault(int(time.perf_counter() - t0), Second())
            slot.ok += 1
            if ttft is not None:
                slot.ttfts.append(ttft)
    except httpx.HTTPError:
        buckets.setdefault(int(time.perf_counter() - t0), Second()).failed += 1


async def drive(args: argparse.Namespace) -> None:
    color = sys.stdout.isatty() and not args.no_color
    c_ok, c_shed, c_dim, c_b, c_r = (OK, SHED, DIM, BOLD, RESET) if color else ("",) * 5

    plan: list[tuple[float, float]] = []
    if args.ramp:
        for level in [float(x) for x in args.ramp.split(",") if x.strip()]:
            plan.append((level, args.hold))
    else:
        plan.append((args.rps, args.duration))

    body = {
        "messages": [{"role": "user", "content": "summarise the tradeoffs of bounded queues"}],
        "max_tokens": args.max_tokens,
        "stream": True,
    }
    buckets: dict[int, Second] = {}
    rng = random.Random(args.seed)
    printed = -1

    print(f"{c_b}offered -> ok / shed, one row per second{c_r}  "
          f"{c_dim}(ok = 200 streamed, shed = 429/503){c_r}\n")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
        tasks: list[asyncio.Task] = []
        t0 = time.perf_counter()
        elapsed = 0.0
        for rps, hold in plan:
            # Absolute arrival schedule -- a chain of relative sleeps drifts badly
            # once the event loop is busy, and silently under-offers.
            next_at, end = elapsed, elapsed + hold
            while True:
                next_at += rng.expovariate(rps)
                if next_at >= end:
                    break
                delay = next_at - (time.perf_counter() - t0)
                if delay > 0:
                    await asyncio.sleep(delay)
                tasks.append(asyncio.create_task(one(client, body, buckets, t0, color)))

                now = int(time.perf_counter() - t0)
                if now > printed and now - 1 in buckets:
                    s = buckets[now - 1]
                    p50 = sorted(s.ttfts)[len(s.ttfts) // 2] if s.ttfts else None
                    print(
                        f"{c_dim}t+{now - 1:02d}{c_r}  offered {s.offered:>3}   "
                        f"{c_ok}ok {bar(s.ok, '#'):<35}{s.ok:>3}{c_r}   "
                        f"{c_shed}shed {bar(s.shed, 'x'):<35}{s.shed:>3}{c_r}"
                        + (f"   {c_dim}ttft p50 {p50:.1f}s{c_r}" if p50 else "")
                    )
                    printed = now
            elapsed = end

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    tot = Second()
    for s in buckets.values():
        tot.offered += s.offered; tot.ok += s.ok; tot.shed += s.shed; tot.failed += s.failed
    print(
        f"\n{c_b}offered {tot.offered}   served {tot.ok}   shed {tot.shed}"
        f"   failed {tot.failed}{c_r}"
    )
    if tot.failed:
        print(f"{c_shed}note: {tot.failed} hard failures -- shed load is not a failure, these are{c_r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="demo.live_traffic", description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--rps", type=float, default=8.0)
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument("--ramp", default=None, help="comma-separated rates, e.g. 4,8,16,32")
    p.add_argument("--hold", type=float, default=6.0, help="seconds per ramp step")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--no-color", action="store_true")
    return p


def main() -> None:
    try:
        asyncio.run(drive(build_parser().parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
