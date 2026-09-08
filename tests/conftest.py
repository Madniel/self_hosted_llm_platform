from __future__ import annotations

import asyncio
import pytest_asyncio
from asgi_lifespan import LifespanManager
from collections.abc import AsyncIterator
from dataclasses import replace
from llmserve.app import create_app
from llmserve.config import Settings

import httpx
import pytest


FAST = Settings(
    backend="mock",
    model="mock-llm",
    max_concurrent_requests=2,
    max_queue_size=8,
    queue_timeout_s=2.0,
    request_timeout_s=10.0,
    max_tokens_cap=32,
    stream_keepalive_s=0.0,
    mock_ttft_ms=5.0,
    mock_itl_ms=1.0,
    mock_jitter=0.0,
    mock_contention=0.0,
    log_json=False,
    log_level="WARNING",
)


@pytest.fixture
def settings() -> Settings:
    return FAST


def make_settings(**overrides) -> Settings:
    return replace(FAST, **overrides)


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client_for(settings):
        yield c


async def _client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, configure_logs=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as http_client:
            http_client.app = app  # type: ignore[attr-defined]
            yield http_client


@pytest_asyncio.fixture
def client_factory():
    """Build a client with per-test settings overrides."""
    import contextlib

    @contextlib.asynccontextmanager
    async def factory(**overrides):
        gen = _client_for(make_settings(**overrides))
        c = await gen.__anext__()
        try:
            yield c
        finally:
            with contextlib.suppress(StopAsyncIteration):
                await gen.__anext__()

    return factory


async def drain_sse(response: httpx.Response) -> list[dict]:
    import json

    events = []
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append({"__done__": True})
            break
        events.append(json.loads(payload))
    return events


@pytest.fixture
def sse_reader():
    return drain_sse


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
