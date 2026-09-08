from __future__ import annotations

import asyncio
import json


from llmserve.streaming import (
    SSE_DONE,
    SSE_KEEPALIVE,
    LatencyRecorder,
    sse_data,
    sse_error,
    with_keepalive,
)


def test_sse_framing():
    frame = sse_data({"a": 1})
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    assert json.loads(frame[6:-2]) == {"a": 1}
    assert SSE_DONE == b"data: [DONE]\n\n"


def test_sse_error_shape():
    payload = json.loads(sse_error("boom", "engine_error")[6:-2])
    assert payload["error"]["code"] == "engine_error"


async def test_keepalive_fills_silent_gaps():
    async def slow():
        await asyncio.sleep(0.12)
        yield b"data: x\n\n"

    frames = [f async for f in with_keepalive(slow(), interval=0.03)]
    assert frames.count(SSE_KEEPALIVE) >= 2
    assert frames[-1] == b"data: x\n\n"


async def test_keepalive_passthrough_when_disabled():
    async def quick():
        yield b"a"
        yield b"b"

    assert [f async for f in with_keepalive(quick(), interval=0)] == [b"a", b"b"]


async def test_keepalive_closes_source_when_consumer_stops():
    closed = asyncio.Event()

    async def source():
        try:
            while True:
                await asyncio.sleep(0.01)
                yield b"tick"
        finally:
            closed.set()

    agen = with_keepalive(source(), interval=1.0)
    await agen.__anext__()
    await agen.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


async def test_latency_recorder_tracks_ttft_and_gaps():
    rec = LatencyRecorder()
    rec.queue_wait_s = 0.01
    assert rec.on_token() is None          # first token: no gap
    await asyncio.sleep(0.02)
    gap = rec.on_token()
    assert gap is not None and gap > 0
    rec.finish("stop")
    assert rec.ttft_s is not None and rec.ttft_s > 0
    assert rec.tokens == 2
    assert rec.duration_s >= rec.ttft_s
    fields = rec.as_log_fields()
    assert fields["outcome"] == "stop" and fields["completion_tokens"] == 2


def test_latency_recorder_handles_zero_token_requests():
    rec = LatencyRecorder()
    rec.finish("error")
    assert rec.ttft_s is None
    assert rec.output_tps == 0.0
    assert rec.as_log_fields()["ttft_ms"] is None
