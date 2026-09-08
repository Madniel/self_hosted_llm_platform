"""CLI: ``python -m loadtest --help``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .harness import LoadConfig, format_table, run_phase, run_sweep


def _levels(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loadtest",
        description="Load-test a streaming LLM endpoint (TTFT / ITL / goodput / shed rate).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--route", choices=("chat", "completions"), default="chat")
    p.add_argument("--model", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument(
        "--concurrency",
        default="8",
        help="closed-loop virtual users; comma-separated for a sweep (e.g. 1,2,4,8,16)",
    )
    p.add_argument(
        "--rps",
        default=None,
        help="open-loop arrival rate; comma-separated for a sweep. Overrides --concurrency",
    )
    p.add_argument("--duration", type=float, default=20.0, help="seconds per phase")
    p.add_argument("--requests", type=int, default=None, help="cap on requests per phase")
    p.add_argument("--warmup", type=float, default=2.0, help="warmup seconds per phase")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--prompt-words", type=int, default=60)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--no-stream", action="store_true", help="use blocking (non-SSE) requests")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--json", dest="json_out", default=None, help="write full results here")
    return p


async def _run(args: argparse.Namespace) -> int:
    cfg = LoadConfig(
        base_url=args.base_url.rstrip("/"),
        route=args.route,
        model=args.model,
        stream=not args.no_stream,
        duration_s=args.duration,
        requests=args.requests,
        warmup_s=args.warmup,
        max_tokens=args.max_tokens,
        prompt_words=args.prompt_words,
        timeout_s=args.timeout,
        api_key=args.api_key,
        seed=args.seed,
    )

    if args.rps:
        levels = _levels(args.rps)
        open_loop = True
    else:
        levels = _levels(args.concurrency)
        open_loop = False

    if len(levels) == 1:
        if open_loop:
            cfg.rps = levels[0]
        else:
            cfg.concurrency = int(levels[0])
        reports = [await run_phase(cfg)]
    else:
        reports = await run_sweep(cfg, levels, open_loop)

    print()
    print(format_table(reports))
    print()

    last = reports[-1]
    if last.server_stats:
        stats = last.server_stats
        print(
            "server admission: "
            f"admitted={stats.get('admitted')} rejected={stats.get('rejected')} "
            f"peak_queue={stats.get('peak_queue_depth')} "
            f"peak_in_flight={stats.get('peak_in_flight')} "
            f"mean_queue_wait={float(stats.get('mean_queue_wait_s', 0)) * 1000:.1f}ms"
        )

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"config": asdict(cfg), "phases": [r.as_dict() for r in reports]},
                indent=2,
                default=str,
            )
        )
        print(f"wrote {path}")

    # Non-zero exit on hard failures (connection errors / 5xx that were not shed),
    # so this can gate CI. Shed load is a success for the server, not a failure.
    return 1 if any(r.failed for r in reports) else 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
