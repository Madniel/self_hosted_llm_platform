"""Backend selection."""

from __future__ import annotations

from ..config import Settings
from .base import InferenceEngine


def build_engine(settings: Settings) -> InferenceEngine:
    """Construct the engine named by ``settings.backend``.

    Each backend is imported inside its own branch rather than at module scope, so
    importing this package never pulls in vLLM (and therefore CUDA) on a machine
    that only runs the mock.
    """
    if settings.backend == "mock":
        from .mock import MockEngine

        return MockEngine(
            model_id=settings.model_id,
            ttft_ms=settings.mock_ttft_ms,
            itl_ms=settings.mock_itl_ms,
            jitter=settings.mock_jitter,
            contention=settings.mock_contention,
            seed=settings.mock_seed,
        )
    if settings.backend == "vllm":
        from .vllm_engine import VLLMEngine

        return VLLMEngine(settings)
    raise ValueError(f"unknown backend {settings.backend!r}")
