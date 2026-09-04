"""The request path: admission -> engine -> metered token stream.

Both the completions and chat-completions routes funnel through here, so latency
accounting, timeouts, cancellation and metrics have exactly one implementation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Iterable, List, Optional

from . import metrics
from .admission import AdmissionController, Lease
from .config import Settings
from .engine.base import GenerationRequest, InferenceEngine, TokenChunk
from .errors import AdmissionError, EngineError, RequestTimeoutError
from .streaming import LatencyRecorder

logger = logging.getLogger(__name__)

# Serialises one text delta into an SSE frame (route-specific wire shape).
DeltaEncoder = Callable[[str, bool], bytes]
# Serialises the terminal frame(s) given a finish reason and usage counts.
FinalEncoder = Callable[[str, int, int], Iterable[bytes]]


@dataclass
class AppContext:
    """Everything a route needs, assembled once during the app lifespan."""

    settings: Settings
    engine: InferenceEngine
    admission: AdmissionController

    @property
    def model_id(self) -> str:
        return self.engine.model_id


@dataclass
class GenerationResult:
    text: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int


async def admit(ctx: AppContext, route: str, request_id: str) -> Lease:
    """Acquire an execution slot or raise, counting the rejection."""
    try:
        return await ctx.admission.acquire(request_id)
    except AdmissionError as exc:
        metrics.rejections_total.labels(route=route, reason=exc.reason).inc()
        logger.warning(
            "request rejected",
            extra={
                "route": route,
                "reason": exc.reason,
                "in_flight": ctx.admission.in_flight,
                "queue_depth": ctx.admission.queue_depth,
            },
        )
        raise


def _observe_start(route: str, rec: LatencyRecorder) -> None:
    metrics.queue_wait_seconds.labels(route=route).observe(rec.queue_wait_s)


def _observe_end(route: str, rec: LatencyRecorder) -> None:
    if rec.ttft_s is not None:
        metrics.ttft_seconds.labels(route=route).observe(rec.ttft_s)
    metrics.request_duration_seconds.labels(route=route, outcome=rec.outcome).observe(
        rec.duration_s
    )
    metrics.requests_total.labels(route=route, outcome=rec.outcome).inc()
    if rec.prompt_tokens:
        metrics.prompt_tokens_total.labels(route=route).inc(rec.prompt_tokens)
    if rec.tokens:
        metrics.generated_tokens_total.labels(route=route).inc(rec.tokens)
    logger.info("request complete", extra={"route": route, **rec.as_log_fields()})


def _account(route: str, rec: LatencyRecorder, chunk: TokenChunk) -> None:
    gap = rec.on_token()
    if gap is not None:
        metrics.inter_token_seconds.labels(route=route).observe(gap)


async def generate_blocking(
    ctx: AppContext,
    request: GenerationRequest,
    route: str,
    rec: LatencyRecorder,
) -> GenerationResult:
    """Run a generation to completion and return the whole thing."""
    _observe_start(route, rec)
    parts: List[str] = []
    finish_reason = "stop"
    prompt_tokens = completion_tokens = 0
    agen = ctx.engine.generate(request)

    async def _drain() -> None:
        nonlocal finish_reason, prompt_tokens, completion_tokens
        async for chunk in agen:
            if chunk.text:
                parts.append(chunk.text)
                _account(route, rec, chunk)
            prompt_tokens = chunk.prompt_tokens or prompt_tokens
            completion_tokens = chunk.completion_tokens or completion_tokens
            if chunk.is_final:
                finish_reason = chunk.finish_reason or finish_reason

    try:
        await asyncio.wait_for(_drain(), timeout=ctx.settings.request_timeout_s)
    except asyncio.TimeoutError as exc:
        await ctx.engine.abort(request.request_id)
        rec.prompt_tokens = prompt_tokens
        rec.finish("timeout")
        _observe_end(route, rec)
        raise RequestTimeoutError(
            f"generation exceeded {ctx.settings.request_timeout_s:g}s"
        ) from exc
    except asyncio.CancelledError:
        await ctx.engine.abort(request.request_id)
        rec.finish("cancelled")
        _observe_end(route, rec)
        raise
    except Exception as exc:
        await ctx.engine.abort(request.request_id)
        rec.finish("error")
        _observe_end(route, rec)
        logger.exception("engine failure", extra={"route": route})
        raise EngineError(f"inference backend failed: {exc}") from exc
    finally:
        await agen.aclose()

    rec.prompt_tokens = prompt_tokens
    rec.finish(finish_reason)
    _observe_end(route, rec)
    return GenerationResult(
        text="".join(parts),
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens or rec.tokens,
    )


async def generate_stream(
    ctx: AppContext,
    request: GenerationRequest,
    route: str,
    rec: LatencyRecorder,
    lease: Lease,
    encode_delta: DeltaEncoder,
    encode_final: FinalEncoder,
) -> AsyncIterator[bytes]:
    """Stream SSE frames, owning the lease for the whole life of the response.

    The lease is acquired by the caller *before* the response starts, so overload is
    still reported as a real HTTP status. It is released here, in ``finally``, which
    also covers the client-disconnect path: Starlette closes this generator, we abort
    the engine request and give the slot back instead of paying for tokens nobody
    will read.
    """
    _observe_start(route, rec)
    deadline = rec.arrived_at + ctx.settings.request_timeout_s
    finish_reason = "stop"
    prompt_tokens = completion_tokens = 0
    first = True
    agen = ctx.engine.generate(request)
    try:
        async for chunk in agen:
            prompt_tokens = chunk.prompt_tokens or prompt_tokens
            completion_tokens = chunk.completion_tokens or completion_tokens
            if chunk.text:
                _account(route, rec, chunk)
                yield encode_delta(chunk.text, first)
                first = False
            if chunk.is_final:
                finish_reason = chunk.finish_reason or finish_reason
                break
            if time.perf_counter() > deadline:
                finish_reason = "timeout"
                await ctx.engine.abort(request.request_id)
                break
        rec.prompt_tokens = prompt_tokens
        rec.finish(finish_reason)
        for frame in encode_final(finish_reason, prompt_tokens, completion_tokens or rec.tokens):
            yield frame
    except asyncio.CancelledError:
        rec.finish("cancelled")
        await ctx.engine.abort(request.request_id)
        raise
    except GeneratorExit:
        # Client hung up mid-stream.
        rec.finish("client_disconnect")
        await ctx.engine.abort(request.request_id)
        raise
    except Exception as exc:
        rec.finish("error")
        logger.exception("engine failure mid-stream", extra={"route": route})
        await ctx.engine.abort(request.request_id)
        from .streaming import sse_error

        yield sse_error(f"inference backend failed: {exc}", "engine_error")
    finally:
        if rec.outcome == "pending":
            rec.finish("aborted")
        await agen.aclose()
        lease.release()
        _observe_end(route, rec)


def build_prompt(messages: Iterable[dict], *, template: Optional[str] = None) -> str:
    """Render chat messages into a single prompt.

    Deliberately minimal and model-agnostic: a production deployment should swap this
    for the tokenizer's own chat template (``tokenizer.apply_chat_template``) so the
    special tokens match what the model was trained on.
    """
    lines: List[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = (message.get("content") or "").strip()
        lines.append(f"{role.capitalize()}: {content}" if content else f"{role.capitalize()}:")
    lines.append("Assistant:")
    return "\n".join(lines)
