from __future__ import annotations

import asyncio
from llmserve.config import Settings
from llmserve.engine import build_engine

from llmserve.engine.base import (
    GenerationRequest,
    SamplingConfig,
    apply_stop_sequences,
    estimate_tokens,
)
from llmserve.engine.mock import MockEngine


def _req(rid="r1", prompt="hello world", **sampling):
    return GenerationRequest(
        request_id=rid, prompt=prompt, sampling=SamplingConfig(**sampling)
    )


async def _collect(engine, request):
    deltas, final = [], None
    async for chunk in engine.generate(request):
        if chunk.is_final:
            final = chunk
        elif chunk.text:
            deltas.append(chunk.text)
    return deltas, final


async def test_mock_engine_emits_requested_number_of_tokens():
    engine = MockEngine(ttft_ms=1, itl_ms=0.1, jitter=0.0)
    await engine.startup()
    deltas, final = await _collect(engine, _req(max_tokens=12))
    assert len(deltas) == 12
    assert final is not None and final.finish_reason == "length"
    assert final.completion_tokens == 12


async def test_mock_engine_is_deterministic_for_a_fixed_seed():
    engine = MockEngine(ttft_ms=1, itl_ms=0.1, jitter=0.0)
    first, _ = await _collect(engine, _req(max_tokens=8, seed=99))
    second, _ = await _collect(engine, _req(rid="other", max_tokens=8, seed=99))
    assert first == second


async def test_mock_engine_honours_stop_sequences():
    engine = MockEngine(ttft_ms=1, itl_ms=0.1, jitter=0.0)
    deltas, final = await _collect(engine, _req(max_tokens=64, stop=("the",), seed=3))
    assert final is not None and final.finish_reason == "stop"
    assert "the" not in "".join(deltas)


async def test_mock_engine_slows_down_under_concurrency():
    """The contention term is what makes queueing visible in a load test.

    Step sizes are kept well above the coarsest timer granularity we care about
    (Windows rounds ``asyncio.sleep`` up to ~15.6 ms), so the effect being measured
    is contention and not the platform's clock.
    """
    engine = MockEngine(ttft_ms=1, itl_ms=30.0, jitter=0.0, contention=1.0)
    tokens = 12
    fanout = 4

    async def run(tag: str):
        await _collect(engine, _req(rid=f"r{tag}", max_tokens=tokens))

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await run("solo")
    solo = loop.time() - t0

    t0 = loop.time()
    await asyncio.gather(*(run(f"c{i}") for i in range(fanout)))
    crowded = loop.time() - t0

    # contention=1.0 with 4 in flight should widen each step ~4x; assert well under
    # that so the test measures the mechanism, not the scheduler's mood.
    assert crowded > solo * 1.5, f"solo={solo:.3f}s crowded={crowded:.3f}s"


async def test_abort_stops_generation_early():
    engine = MockEngine(ttft_ms=1, itl_ms=5.0, jitter=0.0)
    request = _req(rid="abortme", max_tokens=200)

    async def consume():
        count = 0
        async for chunk in engine.generate(request):
            if chunk.is_final:
                return count, chunk.finish_reason
            count += 1
        return count, None

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await engine.abort("abortme")
    count, reason = await asyncio.wait_for(task, timeout=2)
    assert reason == "abort"
    assert count < 200


async def test_active_counter_returns_to_zero_when_consumer_walks_away():
    engine = MockEngine(ttft_ms=1, itl_ms=1.0, jitter=0.0)
    agen = engine.generate(_req(max_tokens=100))
    await agen.__anext__()
    assert engine.active == 1
    await agen.aclose()
    assert engine.active == 0


def test_stop_sequence_helper():
    assert apply_stop_sequences("abc STOP def", ["STOP"]) == ("abc ", True)
    assert apply_stop_sequences("abc", ["STOP"]) == ("abc", False)
    assert apply_stop_sequences("a X b Y", ["Y", "X"]) == ("a ", True)


def test_token_estimate():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") >= 1


def test_factory_selects_backend():
    assert isinstance(build_engine(Settings(backend="mock")), MockEngine)
    try:
        build_engine(Settings(backend="nope"))
    except ValueError as exc:
        assert "unknown backend" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
