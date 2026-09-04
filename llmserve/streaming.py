"""Server-sent-event plumbing and per-request latency accounting."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List, Optional

SSE_DONE = b"data: [DONE]\n\n"
SSE_KEEPALIVE = b": keep-alive\n\n"

# Headers that keep SSE intact through nginx / envoy / cloud load balancers.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_data(payload: Any) -> bytes:
    """Encode one SSE ``data:`` frame."""
    if isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
    elif isinstance(payload, str):
        body = payload.encode()
    else:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return b"data: " + body + b"\n\n"


def sse_error(message: str, code: str, error_type: str = "server_error") -> bytes:
    """Errors that surface *after* the 200 has been sent can only go in-band."""
    return sse_data(
        {"error": {"message": message, "type": error_type, "code": code, "param": None}}
    )


async def with_keepalive(
    source: AsyncIterator[bytes], interval: Optional[float]
) -> AsyncIterator[bytes]:
    """Emit an SSE comment whenever ``source`` is quiet for ``interval`` seconds.

    Long prefills (or a queued request) can leave a stream silent for many seconds;
    proxies happily drop such connections. A comment frame keeps the socket warm and
    is ignored by every conforming SSE client.
    """
    if not interval or interval <= 0:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    pending: Optional[asyncio.Future] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield SSE_KEEPALIVE
                continue
            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


@dataclass
class LatencyRecorder:
    """Accumulates the numbers that matter for interactive serving.

    ``ttft`` is measured from *request arrival*, not from admission, so queueing shows
    up in the metric the user actually feels.
    """

    arrived_at: float = field(default_factory=time.perf_counter)
    queue_wait_s: float = 0.0
    first_token_at: Optional[float] = None
    last_token_at: Optional[float] = None
    finished_at: Optional[float] = None
    inter_token_gaps: List[float] = field(default_factory=list)
    tokens: int = 0
    prompt_tokens: int = 0
    outcome: str = "pending"

    def on_token(self, count: int = 1) -> Optional[float]:
        """Record a delivered delta; returns the inter-token gap when there is one."""
        now = time.perf_counter()
        gap: Optional[float] = None
        if self.first_token_at is None:
            self.first_token_at = now
        else:
            gap = now - (self.last_token_at or now)
            self.inter_token_gaps.append(gap)
        self.last_token_at = now
        self.tokens += count
        return gap

    def finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.finished_at = time.perf_counter()

    @property
    def ttft_s(self) -> Optional[float]:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.arrived_at

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.perf_counter()
        return end - self.arrived_at

    @property
    def decode_s(self) -> float:
        if self.first_token_at is None or self.last_token_at is None:
            return 0.0
        return self.last_token_at - self.first_token_at

    @property
    def output_tps(self) -> float:
        """Decode throughput for this request (tokens after the first)."""
        if self.tokens < 2 or self.decode_s <= 0:
            return 0.0
        return (self.tokens - 1) / self.decode_s

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "queue_wait_ms": round(self.queue_wait_s * 1000, 2),
            "ttft_ms": round(self.ttft_s * 1000, 2) if self.ttft_s is not None else None,
            "duration_ms": round(self.duration_s * 1000, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.tokens,
            "output_tps": round(self.output_tps, 1),
        }
