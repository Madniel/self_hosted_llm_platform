"""Minimal streaming client -- a reference for consuming the SSE API.

Cross-platform (no curl needed) and prints the latency numbers that matter:

    python scripts/stream_client.py --prompt "explain paged attention"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stream one completion and report TTFT / ITL.")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--route", choices=("chat", "completions"), default="chat")
    p.add_argument("--prompt", default="Explain paged attention in two sentences.")
    p.add_argument("--max-tokens", type=int, default=48)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--model", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--timeout", type=float, default=120.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    path = "/v1/chat/completions" if args.route == "chat" else "/v1/completions"
    body = {"max_tokens": args.max_tokens, "temperature": args.temperature, "stream": True}
    if args.model:
        body["model"] = args.model
    if args.route == "chat":
        body["messages"] = [{"role": "user", "content": args.prompt}]
    else:
        body["prompt"] = args.prompt

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    started = time.perf_counter()
    ttft = None
    last = None
    gaps: list[float] = []
    tokens = 0

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.timeout) as client:
        with client.stream("POST", path, json=body, headers=headers) as response:
            if response.status_code != 200:
                response.read()
                print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
                return 1
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                if "error" in event:
                    print(f"\nstream error: {event['error']}", file=sys.stderr)
                    return 1
                choice = (event.get("choices") or [{}])[0]
                text = (
                    (choice.get("delta") or {}).get("content")
                    if args.route == "chat"
                    else choice.get("text")
                ) or ""
                if not text:
                    continue
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - started
                elif last is not None:
                    gaps.append(now - last)
                last = now
                tokens += 1
                print(text, end="", flush=True)

    total = time.perf_counter() - started
    print("\n")
    if ttft is None:
        print("no tokens received")
        return 1
    decode = (last - (started + ttft)) if last else 0.0
    parts = [f"tokens={tokens}", f"ttft={ttft * 1000:.0f}ms"]
    if gaps:
        parts.append(f"mean_itl={sum(gaps) / len(gaps) * 1000:.1f}ms")
    if decode > 0:
        parts.append(f"decode_tps={(tokens - 1) / decode:.1f}")
    parts.append(f"total={total * 1000:.0f}ms")
    print("  ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
