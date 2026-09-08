"""A GPU-free backend that imitates the timing behaviour of a real LLM engine.

It is not a toy: the point is that the *serving* layer -- streaming, admission control,
backpressure, metrics, the load-test harness -- can be exercised end to end on a laptop
and in CI, and that the numbers it produces have the right *shape*:

* a prefill cost that grows with prompt length (TTFT),
* a steady inter-token interval with jitter (ITL),
* and decode that slows down as concurrency rises, because a real batching engine
  splits a fixed compute budget across the batch.

That last property is what makes queueing and admission control observable at all.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Sequence


from .base import (
    GenerationRequest,
    InferenceEngine,
    TokenChunk,
    apply_stop_sequences,
    estimate_tokens,
)

_VOCAB: Sequence[str] = (
    "the model streams tokens back to the caller as soon as the first one is ready "
    "which keeps perceived latency low even when total generation time is long "
    "admission control bounds the number of requests that reach the engine at once "
    "so that queueing is explicit and tail latency stays predictable under load "
    "everything past the bound waits briefly in a fifo queue or is shed immediately "
).split()


class MockEngine(InferenceEngine):
    """Deterministic-per-request, timing-realistic stand-in for vLLM."""

    def __init__(
        self,
        model_id: str = "mock-llm",
        *,
        ttft_ms: float = 120.0,
        itl_ms: float = 18.0,
        jitter: float = 0.25,
        contention: float = 0.06,
        seed: int = 1234,
        prefill_ms_per_kchar: float = 40.0,
    ) -> None:
        self._model_id = model_id
        self._ttft = ttft_ms / 1000.0
        self._itl = itl_ms / 1000.0
        self._jitter = max(0.0, jitter)
        self._contention = max(0.0, contention)
        self._seed = seed
        self._prefill_per_char = prefill_ms_per_kchar / 1000.0 / 1000.0
        self._active = 0
        self._aborted: set[str] = set()
        self._ready = False

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def active(self) -> int:
        return self._active

    async def startup(self) -> None:
        await asyncio.sleep(0)  # stands in for weight loading
        self._ready = True

    async def shutdown(self) -> None:
        self._ready = False
        self._aborted.clear()

    async def abort(self, request_id: str) -> None:
        self._aborted.add(request_id)

    def _delay(self, base: float, rng: random.Random) -> float:
        """Per-step delay, widened by batch contention and jittered."""
        crowd = 1.0 + self._contention * max(0, self._active - 1)
        noise = 1.0 + rng.uniform(-self._jitter, self._jitter) if self._jitter else 1.0
        return max(0.0, base * crowd * noise)

    async def generate(self, request: GenerationRequest) -> AsyncIterator[TokenChunk]:
        sampling = request.sampling
        rng = random.Random(
            sampling.seed if sampling.seed is not None else hash((self._seed, request.request_id))
        )
        prompt_tokens = estimate_tokens(request.prompt)
        self._active += 1
        emitted = 0
        text_so_far = ""
        finish_reason: str | None = "length"
        try:
            # Prefill: fixed overhead plus a term proportional to prompt length.
            await asyncio.sleep(
                self._delay(self._ttft + self._prefill_per_char * len(request.prompt), rng)
            )
            while emitted < sampling.max_tokens:
                if request.request_id in self._aborted:
                    finish_reason = "abort"
                    break
                if emitted:
                    await asyncio.sleep(self._delay(self._itl, rng))
                word = _VOCAB[rng.randrange(len(_VOCAB))]
                piece = word if emitted == 0 else " " + word
                candidate = text_so_far + piece

                trimmed, stopped = apply_stop_sequences(candidate, sampling.stop)
                if stopped:
                    delta = trimmed[len(text_so_far) :]
                    if delta:
                        emitted += 1
                        yield TokenChunk(
                            text=delta,
                            index=emitted - 1,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=emitted,
                        )
                    finish_reason = "stop"
                    break

                text_so_far = candidate
                emitted += 1
                yield TokenChunk(
                    text=piece,
                    index=emitted - 1,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=emitted,
                )
            else:
                finish_reason = "length"

            yield TokenChunk(
                text="",
                index=emitted,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=emitted,
            )
        finally:
            self._active -= 1
            self._aborted.discard(request.request_id)


class SlowStartMockEngine(MockEngine):
    """Mock engine with a configurable startup delay, for readiness-probe testing."""

    def __init__(self, *args, load_seconds: float = 1.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._load_seconds = load_seconds

    async def startup(self) -> None:
        started = time.perf_counter()
        await asyncio.sleep(self._load_seconds)
        self._ready = True
        self._load_took = time.perf_counter() - started
