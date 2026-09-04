"""End-to-end HTTP behaviour over the ASGI app."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import drain_sse


async def test_health_and_models(client):
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["engine_ready"] is True

    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True

    models = await client.get("/v1/models")
    assert models.json()["data"][0]["id"] == "mock-llm"

    assert "X-Request-Id" in health.headers


async def test_blocking_completion(client):
    resp = await client.post(
        "/v1/completions", json={"prompt": "hello", "max_tokens": 10, "stream": False}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"]
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["usage"]["completion_tokens"] == 10
    assert body["usage"]["total_tokens"] >= 10


async def test_blocking_chat_completion(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 6},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]


async def test_streaming_completion_yields_incremental_deltas(client):
    async with client.stream(
        "POST",
        "/v1/completions",
        json={"prompt": "hello", "max_tokens": 8, "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        events = await drain_sse(resp)

    assert events[-1] == {"__done__": True}
    deltas = [e for e in events[:-1] if e["choices"][0].get("text")]
    assert len(deltas) == 8
    final = events[-2]
    assert final["choices"][0]["finish_reason"] == "length"
    assert final["usage"]["completion_tokens"] == 8


async def test_streaming_chat_sends_role_once_then_content(client):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5, "stream": True},
    ) as resp:
        events = await drain_sse(resp)

    chunks = [e for e in events[:-1]]
    roles = [c["choices"][0]["delta"].get("role") for c in chunks]
    assert roles[0] == "assistant"
    assert all(r is None for r in roles[1:])
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


async def test_max_tokens_is_clamped_to_server_policy(client):
    resp = await client.post(
        "/v1/completions", json={"prompt": "hello", "max_tokens": 10_000}
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["completion_tokens"] == 32  # max_tokens_cap


async def test_stop_sequence_is_applied(client):
    """Generate once, then replay the same seed with a word from the output as a stop."""
    baseline = await client.post(
        "/v1/completions", json={"prompt": "hello", "max_tokens": 32, "seed": 11}
    )
    text = baseline.json()["choices"][0]["text"]
    stop_word = text.split()[3]

    resp = await client.post(
        "/v1/completions",
        json={"prompt": "hello", "max_tokens": 32, "stop": [stop_word], "seed": 11},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert stop_word not in body["choices"][0]["text"]
    assert len(body["choices"][0]["text"]) < len(text)


async def test_validation_errors_use_openai_error_shape(client):
    resp = await client.post("/v1/completions", json={"prompt": "", "max_tokens": 4})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["type"] == "invalid_request_error"

    resp = await client.post("/v1/completions", json={"prompt": "x", "max_tokens": 0})
    assert resp.status_code == 400


async def test_unknown_model_is_rejected(client):
    resp = await client.post(
        "/v1/completions", json={"prompt": "x", "model": "llama-3-70b"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_prompt_length_limit(client_factory):
    async with client_factory(max_prompt_chars=32) as client:
        resp = await client.post("/v1/completions", json={"prompt": "x" * 100})
        assert resp.status_code == 400
        assert "character limit" in resp.json()["error"]["message"]


async def test_overload_sheds_with_429_and_retry_after(client_factory):
    """One slot, no queue: the second concurrent request must be refused, not stalled."""
    async with client_factory(
        max_concurrent_requests=1,
        max_queue_size=0,
        mock_ttft_ms=200.0,
        mock_itl_ms=5.0,
    ) as client:
        payload = {"prompt": "hello", "max_tokens": 16}
        first = asyncio.create_task(client.post("/v1/completions", json=payload))
        await asyncio.sleep(0.05)
        second = await client.post("/v1/completions", json=payload)

        assert second.status_code == 429
        body = second.json()["error"]
        assert body["code"] == "queue_full"
        assert int(second.headers["Retry-After"]) >= 1
        assert (await first).status_code == 200


async def test_queued_requests_are_served_in_order(client_factory):
    async with client_factory(
        max_concurrent_requests=1, max_queue_size=4, mock_ttft_ms=30.0, mock_itl_ms=1.0
    ) as client:
        payload = {"prompt": "hello", "max_tokens": 4}
        responses = await asyncio.gather(
            *(client.post("/v1/completions", json=payload) for _ in range(4))
        )
        assert [r.status_code for r in responses] == [200] * 4

        stats = (await client.get("/stats")).json()["admission"]
        assert stats["admitted"] == 4
        assert stats["peak_queue_depth"] >= 1
        assert stats["in_flight"] == 0


async def test_queue_timeout_returns_503(client_factory):
    async with client_factory(
        max_concurrent_requests=1,
        max_queue_size=4,
        queue_timeout_s=0.05,
        mock_ttft_ms=400.0,
    ) as client:
        payload = {"prompt": "hello", "max_tokens": 8}
        first = asyncio.create_task(client.post("/v1/completions", json=payload))
        await asyncio.sleep(0.02)
        second = await client.post("/v1/completions", json=payload)
        assert second.status_code == 503
        assert second.json()["error"]["code"] == "queue_timeout"
        await first


async def test_request_timeout_returns_504(client_factory):
    async with client_factory(
        request_timeout_s=0.05, mock_ttft_ms=1.0, mock_itl_ms=20.0
    ) as client:
        resp = await client.post(
            "/v1/completions", json={"prompt": "hello", "max_tokens": 32}
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "request_timeout"


async def test_slot_is_released_after_every_request(client_factory):
    async with client_factory(max_concurrent_requests=1, max_queue_size=2) as client:
        for _ in range(3):
            await client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 3})
        stats = (await client.get("/stats")).json()["admission"]
        assert stats["in_flight"] == 0
        assert stats["queue_depth"] == 0
        assert stats["completed"] == 3


async def test_streaming_slot_is_released_when_client_disconnects(client_factory):
    async with client_factory(
        max_concurrent_requests=1, max_queue_size=0, mock_itl_ms=20.0
    ) as client:
        async with client.stream(
            "POST",
            "/v1/completions",
            json={"prompt": "hello", "max_tokens": 500, "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    break  # walk away mid-stream

        for _ in range(20):
            await asyncio.sleep(0.02)
            stats = (await client.get("/stats")).json()["admission"]
            if stats["in_flight"] == 0:
                break
        assert stats["in_flight"] == 0

        follow_up = await client.post(
            "/v1/completions", json={"prompt": "hi", "max_tokens": 2}
        )
        assert follow_up.status_code == 200


async def test_api_key_enforced_when_configured(client_factory):
    async with client_factory(api_keys=frozenset({"sk-test"})) as client:
        payload = {"prompt": "hi", "max_tokens": 2}
        assert (await client.post("/v1/completions", json=payload)).status_code == 401

        ok = await client.post(
            "/v1/completions", json=payload, headers={"Authorization": "Bearer sk-test"}
        )
        assert ok.status_code == 200

        bad = await client.post(
            "/v1/completions", json=payload, headers={"Authorization": "Bearer nope"}
        )
        assert bad.status_code == 401


async def test_metrics_expose_serving_signals(client):
    await client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 4})
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for metric in (
        "llmserve_requests_total",
        "llmserve_ttft_seconds",
        "llmserve_queue_wait_seconds",
        "llmserve_inter_token_seconds",
        "llmserve_generated_tokens_total",
        "llmserve_max_concurrency",
    ):
        assert metric in body


async def test_rejections_are_counted_in_metrics(client_factory):
    async with client_factory(
        max_concurrent_requests=1, max_queue_size=0, mock_ttft_ms=200.0
    ) as client:
        payload = {"prompt": "hello", "max_tokens": 8}
        first = asyncio.create_task(client.post("/v1/completions", json=payload))
        await asyncio.sleep(0.05)
        await client.post("/v1/completions", json=payload)
        await first
        body = (await client.get("/metrics")).text
        assert 'llmserve_rejections_total{reason="queue_full"' in body


async def test_readyz_reports_not_ready_during_shutdown(client):
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    await ctx.admission.close(drain_timeout=1)
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["accepting"] is False


async def test_drain_endpoint_flips_readiness_but_finishes_in_flight_work(client_factory):
    async with client_factory(max_concurrent_requests=2, mock_ttft_ms=150.0) as client:
        payload = {"prompt": "hello", "max_tokens": 8}
        in_flight = asyncio.create_task(client.post("/v1/completions", json=payload))
        await asyncio.sleep(0.05)

        drain = await client.post("/admin/drain")
        assert drain.status_code == 200
        assert drain.json()["accepting"] is False

        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/healthz")).status_code == 200   # still alive

        refused = await client.post("/v1/completions", json=payload)
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "shutting_down"

        assert (await in_flight).status_code == 200                # finished cleanly
