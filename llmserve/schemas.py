"""OpenAI-compatible request/response models.

Sticking to the OpenAI wire format means existing clients (openai-python, LangChain,
curl snippets, load generators) work against this server unchanged.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, List, Literal, Optional, Union

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
    stop: Union[str, List[str], None] = None
    seed: Optional[int] = None
    ignore_eos: bool = False
    stream: bool = False
    user: Optional[str] = None

    @field_validator("stop")
    @classmethod
    def _normalise_stop(cls, value: Union[str, List[str], None]) -> Optional[List[str]]:
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
    name: Optional[str] = None


class CompletionRequest(SamplingFields):
    model: Optional[str] = None
    prompt: Union[str, List[str]]

    @field_validator("prompt")
    @classmethod
    def _single_prompt(cls, value: Union[str, List[str]]) -> str:
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("batched prompts are not supported; send one prompt per request")
            value = value[0]
        if not value.strip():
            raise ValueError("prompt must not be empty")
        return value


class ChatCompletionRequest(SamplingFields):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(min_length=1)


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
    finish_reason: Optional[str] = None
    logprobs: Optional[Any] = None


class CompletionResponse(_Base):
    id: str = Field(default_factory=lambda: _new_id("cmpl"))
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[CompletionChoice]
    usage: Usage


class ChatChoiceMessage(_Base):
    role: Role = "assistant"
    content: str = ""


class ChatCompletionChoice(_Base):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(_Base):
    id: str = Field(default_factory=lambda: _new_id("chatcmpl"))
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage


# -------------------------------------------------------------------------- streams
class CompletionStreamChoice(_Base):
    index: int = 0
    text: str = ""
    finish_reason: Optional[str] = None


class CompletionStreamChunk(_Base):
    id: str
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[CompletionStreamChoice]
    usage: Optional[Usage] = None


class ChatDelta(_Base):
    role: Optional[Role] = None
    content: Optional[str] = None


class ChatCompletionStreamChoice(_Base):
    index: int = 0
    delta: ChatDelta = Field(default_factory=ChatDelta)
    finish_reason: Optional[str] = None


class ChatCompletionStreamChunk(_Base):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionStreamChoice]
    usage: Optional[Usage] = None


# ----------------------------------------------------------------------- meta / ops
class ModelCard(_Base):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "self-hosted"


class ModelList(_Base):
    object: Literal["list"] = "list"
    data: List[ModelCard]


class HealthResponse(_Base):
    status: str
    model: str
    backend: str
    engine_ready: bool


class StatsResponse(_Base):
    model: str
    backend: str
    admission: dict[str, Any]
