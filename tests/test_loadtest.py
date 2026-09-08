"""The harness must produce correct statistics -- test it against the real app."""

from __future__ import annotations

from asgi_lifespan import LifespanManager
from llmserve.app import create_app

import httpx
import pytest

from loadtest.harness import (
    LoadConfig,
    RequestOutcome,
    _build_report,
    format_table,
    percentile,
    run_phase,
)
from tests.conftest import make_settings


def test_percentiles_interpolate():
    values = [1, 2, 3, 4, 5]
    assert percentile(values, 0) == 1
    assert percentile(values, 50) == 3
    assert percentile(values, 100) == 5
    assert percentile([], 50) != percentile([], 50)  # nan


def test_report_separates_shed_load_from_real_failures():
    outcomes = [
        RequestOutcome(ok=True, status=200, e2e_s=1.0, ttft_s=0.2, tokens=10, itls=[0.05] * 9),
        RequestOutcome(ok=True, status=200, e2e_s=2.0, ttft_s=0.4, tokens=10, itls=[0.1] * 9),
        RequestOutcome(ok=False, status=429, e2e_s=0.01, rejected=True, reject_reason="queue_full"),
        RequestOutcome(ok=False, status=0, e2e_s=0.5, error="ReadTimeout"),
    ]
    report = _build_report(LoadConfig(), "c=2", outcomes, wall=2.0, server_stats=None)
    assert report.issued == 4
    assert report.succeeded == 2
    assert report.rejected == 1
    assert report.failed == 1
    assert report.goodput_rps == pytest.approx(1.0)
    assert report.output_tokens == 20
    assert report.output_tps == pytest.approx(10.0)
    assert report.ttft["p50"] == pytest.approx(0.3)
    assert report.reject_reasons == {"queue_full": 1}
    assert "429" in report.status_codes
    assert "gput/s" in format_table([report])


@pytest.mark.parametrize("stream", [True, False])
async def test_harness_drives_the_real_app(stream):
    settings = make_settings(
        max_concurrent_requests=4, max_queue_size=16, mock_ttft_ms=2.0, mock_itl_ms=0.5
    )
    app = create_app(settings, configure_logs=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        original = httpx.AsyncClient

        class ASGIClient(original):  # route the harness through the in-process app
            def __init__(self, **kwargs):
                kwargs["transport"] = transport
                super().__init__(**kwargs)

        httpx.AsyncClient = ASGIClient  # type: ignore[misc]
        try:
            report = await run_phase(
                LoadConfig(
                    base_url="http://test",
                    concurrency=4,
                    duration_s=0.8,
                    warmup_s=0.1,
                    max_tokens=6,
                    prompt_words=8,
                    stream=stream,
                )
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    assert report.succeeded > 0
    assert report.failed == 0
    assert report.goodput_rps > 0
    assert report.server_stats is not None
    assert report.server_stats["admitted"] >= report.succeeded


async def test_harness_reports_shed_load_under_overload():
    settings = make_settings(
        max_concurrent_requests=1, max_queue_size=0, mock_ttft_ms=25.0, mock_itl_ms=2.0
    )
    app = create_app(settings, configure_logs=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        original = httpx.AsyncClient

        class ASGIClient(original):
            def __init__(self, **kwargs):
                kwargs["transport"] = transport
                super().__init__(**kwargs)

        httpx.AsyncClient = ASGIClient  # type: ignore[misc]
        try:
            report = await run_phase(
                LoadConfig(
                    base_url="http://test",
                    concurrency=16,
                    duration_s=1.0,
                    warmup_s=0.0,
                    max_tokens=8,
                    prompt_words=6,
                )
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    assert report.rejected > 0, "expected admission control to shed load"
    assert report.failed == 0, "shed load must not surface as connection errors"
    assert report.reject_reasons.get("queue_full", 0) > 0
