"""vLLM ``AsyncLLMEngine`` adapter.

vLLM's async engine yields *cumulative* outputs on every scheduler step; this adapter
converts them into deltas so the serving layer only ever forwards new text. vLLM is
imported lazily so the rest of the package installs and tests without CUDA present.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .base import GenerationRequest, InferenceEngine, TokenChunk


if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

logger = logging.getLogger(__name__)

_FINISH_MAP = {"length": "length", "stop": "stop", "abort": "abort"}


class VLLMEngine(InferenceEngine):
    """Wraps :class:`vllm.AsyncLLMEngine` behind the project's engine contract."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._engine: Any = None
        self._sampling_cls: Any = None
        self._ready = False

    @property
    def model_id(self) -> str:
        return self._settings.model_id

    @property
    def ready(self) -> bool:
        return self._ready

    async def startup(self) -> None:
        try:
            from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
        except ImportError as exc:  # pragma: no cover - depends on host
            raise RuntimeError(
                "backend='vllm' requires the vllm package on a CUDA host "
                "(pip install -r requirements-gpu.txt). Use LLMSERVE_BACKEND=mock "
                "to run without a GPU."
            ) from exc

        s = self._settings
        args = AsyncEngineArgs(
            model=s.model,
            served_model_name=s.served_model_name or None,
            tensor_parallel_size=s.vllm_tensor_parallel_size,
            gpu_memory_utilization=s.vllm_gpu_memory_utilization,
            max_model_len=s.vllm_max_model_len,
            dtype=s.vllm_dtype,
            enforce_eager=s.vllm_enforce_eager,
            trust_remote_code=s.vllm_trust_remote_code,
            swap_space=s.vllm_swap_space_gb,
            disable_log_requests=True,
            # vLLM does its own continuous batching; our admission controller bounds
            # how many requests are handed to it, so keep its own queue shallow.
            max_num_seqs=max(1, s.max_concurrent_requests),
        )
        logger.info("loading vLLM engine", extra={"model": s.model})
        self._engine = AsyncLLMEngine.from_engine_args(args)
        self._sampling_cls = SamplingParams
        self._ready = True
        logger.info("vLLM engine ready", extra={"model": s.model})

    async def shutdown(self) -> None:
        self._ready = False
        engine, self._engine = self._engine, None
        if engine is None:
            return
        shutdown = getattr(engine, "shutdown_background_loop", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # pragma: no cover - best effort
                logger.warning("vLLM shutdown raised", exc_info=True)

    def _to_sampling_params(self, request: GenerationRequest) -> Any:
        s = request.sampling
        return self._sampling_cls(
            n=1,
            max_tokens=s.max_tokens,
            temperature=s.temperature,
            top_p=s.top_p,
            top_k=s.top_k,
            presence_penalty=s.presence_penalty,
            frequency_penalty=s.frequency_penalty,
            repetition_penalty=s.repetition_penalty,
            stop=list(s.stop) or None,
            seed=s.seed,
            ignore_eos=s.ignore_eos,
        )

    async def generate(self, request: GenerationRequest) -> AsyncIterator[TokenChunk]:
        if self._engine is None:
            raise RuntimeError("vLLM engine is not started")

        params = self._to_sampling_params(request)
        sent = 0            # characters already forwarded
        index = 0           # delta counter
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = None

        try:
            async for output in self._engine.generate(
                request.prompt, params, request.request_id
            ):
                if not output.outputs:
                    continue
                completion = output.outputs[0]
                prompt_tokens = len(getattr(output, "prompt_token_ids", ()) or ())
                completion_tokens = len(getattr(completion, "token_ids", ()) or ())
                text = completion.text
                if len(text) > sent:
                    delta, sent = text[sent:], len(text)
                    yield TokenChunk(
                        text=delta,
                        index=index,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                    index += 1
                if completion.finish_reason:
                    finish_reason = _FINISH_MAP.get(
                        completion.finish_reason, completion.finish_reason
                    )
        except BaseException:
            # Client disconnect / timeout: free the sequence slot immediately rather
            # than letting the engine finish work nobody is waiting for.
            await self.abort(request.request_id)
            raise

        yield TokenChunk(
            text="",
            index=index,
            finish_reason=finish_reason or "stop",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def abort(self, request_id: str) -> None:
        if self._engine is None:
            return
        try:
            await self._engine.abort(request_id)
        except Exception:  # pragma: no cover - engine may already have finished it
            logger.debug("abort failed for %s", request_id, exc_info=True)
