"""OpenAI-compatible inference endpoints.

Both endpoints follow the same shape:

    validate -> admit (may 429/503) -> generate -> stream or collect -> release

Admission happens *before* a response is started, which is what lets overload be
reported as a real HTTP status instead of an error buried inside a 200 stream.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..engine.base import GenerationRequest, SamplingConfig
from ..errors import InvalidRequestError, ModelNotFoundError
from ..logging_setup import new_request_id, request_id_var
from ..schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamChoice,
    ChatCompletionStreamChunk,
    ChatChoiceMessage,
    ChatDelta,
    CompletionChoice,
    CompletionRequest,
    CompletionResponse,
    CompletionStreamChoice,
    CompletionStreamChunk,
    SamplingFields,
    Usage,
    _new_id,
)
from ..service import AppContext, admit, build_prompt, generate_blocking, generate_stream
from ..streaming import SSE_DONE, SSE_HEADERS, LatencyRecorder, sse_data, with_keepalive
from .deps import get_ctx, require_api_key

router = APIRouter(tags=["inference"], dependencies=[Depends(require_api_key)])


# ------------------------------------------------------------------ validation
def _check_model(requested: str | None, ctx: AppContext) -> None:
    if requested in (None, "", "default", ctx.model_id, ctx.settings.model):
        return
    raise ModelNotFoundError(
        f"model {requested!r} is not served here; this server serves {ctx.model_id!r}"
    )


def _check_prompt(prompt: str, ctx: AppContext) -> None:
    limit = ctx.settings.max_prompt_chars
    if len(prompt) > limit:
        raise InvalidRequestError(
            f"prompt is {len(prompt)} characters, over the {limit} character limit"
        )


def _sampling(body: SamplingFields, ctx: AppContext) -> SamplingConfig:
    """Clamp client-supplied limits to server policy.

    ``max_tokens`` is clamped rather than rejected: a client asking for more than the
    server allows still gets a useful answer, and the cap keeps any single request
    from monopolising a slot.
    """
    return SamplingConfig(
        max_tokens=min(body.max_tokens, ctx.settings.max_tokens_cap),
        temperature=body.temperature,
        top_p=body.top_p,
        top_k=body.top_k,
        presence_penalty=body.presence_penalty,
        frequency_penalty=body.frequency_penalty,
        repetition_penalty=body.repetition_penalty,
        stop=body.stop_tuple(),
        seed=body.seed,
        ignore_eos=body.ignore_eos,
    )


def _request_id(request: Request) -> str:
    return request_id_var.get() or getattr(request.state, "request_id", None) or new_request_id()


def _stream_headers(request_id: str) -> dict:
    return {**SSE_HEADERS, "X-Request-Id": request_id}


# ----------------------------------------------------------------- /v1/completions
@router.post("/v1/completions", response_model=None)
async def create_completion(
    body: CompletionRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
):
    """Text completion, streaming or buffered.

    The lease is acquired before either response is built, and released on every
    exit path: in the buffered case by the ``finally``, in the streaming case by
    ``generate_stream`` once the body is exhausted -- with the ``except`` here
    covering the narrow window where constructing the response itself fails.
    """
    route = "completions"
    _check_model(body.model, ctx)
    _check_prompt(body.prompt, ctx)

    request_id = _request_id(request)
    completion_id = _new_id("cmpl")
    model = ctx.model_id
    gen_req = GenerationRequest(
        request_id=request_id, prompt=body.prompt, sampling=_sampling(body, ctx)
    )

    recorder = LatencyRecorder()
    lease = await admit(ctx, route, request_id)
    recorder.queue_wait_s = lease.queue_wait_s

    if not body.stream:
        try:
            result = await generate_blocking(ctx, gen_req, route, recorder)
        finally:
            lease.release()
        return CompletionResponse(
            id=completion_id,
            model=model,
            choices=[
                CompletionChoice(
                    index=0, text=result.text, finish_reason=result.finish_reason
                )
            ],
            usage=Usage.of(result.prompt_tokens, result.completion_tokens),
        )

    def encode_delta(text: str, _first: bool) -> bytes:
        return sse_data(
            CompletionStreamChunk(
                id=completion_id,
                model=model,
                choices=[CompletionStreamChoice(index=0, text=text)],
            ).model_dump(exclude_none=True)
        )

    def encode_final(finish_reason: str, prompt_tokens: int, tokens: int) -> Iterable[bytes]:
        yield sse_data(
            CompletionStreamChunk(
                id=completion_id,
                model=model,
                choices=[
                    CompletionStreamChoice(index=0, text="", finish_reason=finish_reason)
                ],
                usage=Usage.of(prompt_tokens, tokens),
            ).model_dump(exclude_none=True)
        )
        yield SSE_DONE

    try:
        frames = generate_stream(
            ctx, gen_req, route, recorder, lease, encode_delta, encode_final
        )
        return StreamingResponse(
            with_keepalive(frames, ctx.settings.stream_keepalive_s),
            media_type="text/event-stream",
            headers=_stream_headers(request_id),
        )
    except BaseException:
        lease.release()
        raise


# ------------------------------------------------------------ /v1/chat/completions
@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
):
    """Chat completion, streaming or buffered.

    Identical in shape to :func:`create_completion`; the differences are that the
    messages are flattened into a prompt first, and that the streaming encoders emit
    the chat wire format, where the first delta carries the ``assistant`` role.
    """
    route = "chat"
    _check_model(body.model, ctx)
    prompt = build_prompt([m.model_dump() for m in body.messages])
    _check_prompt(prompt, ctx)

    request_id = _request_id(request)
    completion_id = _new_id("chatcmpl")
    model = ctx.model_id
    gen_req = GenerationRequest(
        request_id=request_id, prompt=prompt, sampling=_sampling(body, ctx)
    )

    recorder = LatencyRecorder()
    lease = await admit(ctx, route, request_id)
    recorder.queue_wait_s = lease.queue_wait_s

    if not body.stream:
        try:
            result = await generate_blocking(ctx, gen_req, route, recorder)
        finally:
            lease.release()
        return ChatCompletionResponse(
            id=completion_id,
            model=model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatChoiceMessage(role="assistant", content=result.text),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=Usage.of(result.prompt_tokens, result.completion_tokens),
        )

    def encode_delta(text: str, first: bool) -> bytes:
        delta = ChatDelta(role="assistant", content=text) if first else ChatDelta(content=text)
        return sse_data(
            ChatCompletionStreamChunk(
                id=completion_id,
                model=model,
                choices=[ChatCompletionStreamChoice(index=0, delta=delta)],
            ).model_dump(exclude_none=True)
        )

    def encode_final(finish_reason: str, prompt_tokens: int, tokens: int) -> Iterable[bytes]:
        yield sse_data(
            ChatCompletionStreamChunk(
                id=completion_id,
                model=model,
                choices=[
                    ChatCompletionStreamChoice(
                        index=0, delta=ChatDelta(), finish_reason=finish_reason
                    )
                ],
                usage=Usage.of(prompt_tokens, tokens),
            ).model_dump(exclude_none=True)
        )
        yield SSE_DONE

    try:
        frames = generate_stream(
            ctx, gen_req, route, recorder, lease, encode_delta, encode_final
        )
        return StreamingResponse(
            with_keepalive(frames, ctx.settings.stream_keepalive_s),
            media_type="text/event-stream",
            headers=_stream_headers(request_id),
        )
    except BaseException:
        lease.release()
        raise


__all__: list[str] = ["router"]
