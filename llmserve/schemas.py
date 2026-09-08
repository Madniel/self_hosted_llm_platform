"""OpenAI-compatible request/response models.

Sticking to the OpenAI wire format means existing clients (openai-python, LangChain,
curl snippets, load generators) work against this server unchanged.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Role = Literal["system", "user", "assistant", "tool"]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------- shared
class SamplingFields(_Base):
    max_tokens: int = Field(default=128, ge=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    top_k: int = Field(default=-1, ge=-1)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    repetition_penalty: float = Field(default=1.0, gt=0.0)
    stop: str | list[str] | None = None
    seed: int | None = None
    ignore_eos: bool = False
    stream: bool = False
    user: str | None = None

    @field_validator("stop")
    @classmethod
    def _normalise_stop(cls, value: str | list[str] | None) -> list[str] | None:
        if value is None:
            return None
        items = [value] if isinstance(value, str) else list(value)
        cleaned = [s for s in items if s]
        if len(cleaned) > 8:
            raise ValueError("at most 8 stop sequences are supported")
        return cleaned or None

    def stop_tuple(self) -> tuple[str, ...]:
        return tuple(self.stop or ())


class ChatMessage(_Base):
    role: Role
    content: str = ""
    name: str | None = None


class CompletionRequest(SamplingFields):
    model: str | None = None
    prompt: str | list[str]

    @field_validator("prompt")
    @classmethod
    def _single_prompt(cls, value: str | list[str]) -> str:
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("batched prompts are not supported; send one prompt per request")
            value = value[0]
        if not value.strip():
            raise ValueError("prompt must not be empty")
        return value


class ChatCompletionRequest(SamplingFields):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)


# ------------------------------------------------------------------------ responses
class Usage(_Base):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def of(cls, prompt: int, completion: int) -> "Usage":
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )


class CompletionChoice(_Base):
    index: int = 0
    text: str = ""
    finish_reason: str | None = None
    logprobs: Any | None = None


class CompletionResponse(_Base):
    id: str = Field(default_factory=lambda: _new_id("cmpl"))
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[CompletionChoice]
    usage: Usage


class ChatChoiceMessage(_Base):
    role: Role = "assistant"
    content: str = ""


class ChatCompletionChoice(_Base):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str | None = None


class ChatCompletionResponse(_Base):
    id: str = Field(default_factory=lambda: _new_id("chatcmpl"))
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage


# -------------------------------------------------------------------------- streams
class CompletionStreamChoice(_Base):
    index: int = 0
    text: str = ""
    finish_reason: str | None = None


class CompletionStreamChunk(_Base):
    id: str
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[CompletionStreamChoice]
    usage: Usage | None = None


class ChatDelta(_Base):
    role: Role | None = None
    content: str | None = None


class ChatCompletionStreamChoice(_Base):
    index: int = 0
    delta: ChatDelta = Field(default_factory=ChatDelta)
    finish_reason: str | None = None


class ChatCompletionStreamChunk(_Base):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionStreamChoice]
    usage: Usage | None = None


# ----------------------------------------------------------------------- meta / ops
class ModelCard(_Base):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "self-hosted"


class ModelList(_Base):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class HealthResponse(_Base):
    status: str
    model: str
    backend: str
    engine_ready: bool


class StatsResponse(_Base):
    model: str
    backend: str
    admission: dict[str, Any]
