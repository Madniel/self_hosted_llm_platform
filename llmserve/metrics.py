"""Prometheus instrumentation for the serving path.

Histogram buckets are chosen for interactive LLM serving: sub-second TTFT matters,
inter-token latency lives in the milliseconds, end-to-end runs into minutes.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

_TTFT_BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0)
_ITL_BUCKETS = (0.005, 0.01, 0.02, 0.035, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0)
_E2E_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0, 180.0, 600.0)
_QUEUE_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

requests_total = Counter(
    "llmserve_requests_total",
    "Requests that were admitted and reached a terminal state.",
    ("route", "outcome"),
    registry=REGISTRY,
)
rejections_total = Counter(
    "llmserve_rejections_total",
    "Requests refused by admission control before reaching the engine.",
    ("route", "reason"),
    registry=REGISTRY,
)
queue_wait_seconds = Histogram(
    "llmserve_queue_wait_seconds",
    "Time a request spent waiting for an execution slot.",
    ("route",),
    buckets=_QUEUE_BUCKETS,
    registry=REGISTRY,
)
ttft_seconds = Histogram(
    "llmserve_ttft_seconds",
    "Time to first token, measured from request arrival (queue wait included).",
    ("route",),
    buckets=_TTFT_BUCKETS,
    registry=REGISTRY,
)
inter_token_seconds = Histogram(
    "llmserve_inter_token_seconds",
    "Gap between consecutive generated tokens.",
    ("route",),
    buckets=_ITL_BUCKETS,
    registry=REGISTRY,
)
request_duration_seconds = Histogram(
    "llmserve_request_duration_seconds",
    "End-to-end request duration.",
    ("route", "outcome"),
    buckets=_E2E_BUCKETS,
    registry=REGISTRY,
)
in_flight = Gauge(
    "llmserve_in_flight_requests",
    "Requests currently executing on the engine.",
    registry=REGISTRY,
)
queue_depth = Gauge(
    "llmserve_queue_depth",
    "Requests currently waiting for an execution slot.",
    registry=REGISTRY,
)
capacity = Gauge(
    "llmserve_max_concurrency",
    "Configured maximum number of concurrently executing requests.",
    registry=REGISTRY,
)
prompt_tokens_total = Counter(
    "llmserve_prompt_tokens_total",
    "Prompt tokens processed.",
    ("route",),
    registry=REGISTRY,
)
generated_tokens_total = Counter(
    "llmserve_generated_tokens_total",
    "Tokens generated and delivered to clients.",
    ("route",),
    registry=REGISTRY,
)
engine_up = Gauge(
    "llmserve_engine_up",
    "1 when the inference engine is loaded and ready.",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Return the metrics exposition body and its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
