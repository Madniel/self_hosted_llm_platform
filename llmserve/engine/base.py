"""Engine-agnostic contract for streaming text generation.

Routes, admission control and metrics all speak this interface, so swapping vLLM for a
mock (or for a remote OpenAI-compatible upstream) never touches the serving layer.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SamplingConfig:
    max_tokens: int = 128
    temperature: float = 0.7
    top_p: float = 1.0
    top_k: int = -1
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    repetition_penalty: float = 1.0
    stop: Sequence[str] = field(default_factory=tuple)
    seed: int | None = None
    ignore_eos: bool = False


@dataclass(frozen=True)
class GenerationRequest:
    request_id: str
    prompt: str
    sampling: SamplingConfig


@dataclass(frozen=True)
class TokenChunk:
    """One incremental step of a generation.

    ``text`` is the *delta* since the previous chunk, never the cumulative output --
    callers can forward it to the wire without diffing.
    """

    text: str
    index: int
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def is_final(self) -> bool:
        return self.finish_reason is not None


class InferenceEngine(abc.ABC):
    """Minimal async engine surface."""

    @property
    @abc.abstractmethod
    def model_id(self) -> str: ...

    @property
    def ready(self) -> bool:
        return True

    async def startup(self) -> None:
        """Load weights / warm caches. Called once from the app lifespan."""

    async def shutdown(self) -> None:
        """Release engine resources."""

    @abc.abstractmethod
    def generate(self, request: GenerationRequest) -> AsyncIterator[TokenChunk]:
        """Yield token deltas until a chunk with a ``finish_reason`` is produced."""

    async def abort(self, request_id: str) -> None:
        """Best-effort cancellation of an in-flight generation."""


def apply_stop_sequences(text: str, stop: Sequence[str]) -> tuple[str, bool]:
    """Truncate ``text`` at the earliest stop sequence. Returns (text, hit)."""
    cut = len(text)
    hit = False
    for token in stop:
        if not token:
            continue
        idx = text.find(token)
        if idx != -1 and idx < cut:
            cut, hit = idx, True
    return (text[:cut], hit) if hit else (text, False)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate for backends that do not report real counts."""
    if not text:
        return 0
    return max(1, len(text) // 4)


__all__: list[str] = [
    "GenerationRequest",
    "InferenceEngine",
    "SamplingConfig",
    "TokenChunk",
    "apply_stop_sequences",
    "estimate_tokens",
]
