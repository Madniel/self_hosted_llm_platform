"""Environment-driven configuration.

Every knob is settable through an ``LLMSERVE_``-prefixed environment variable so the
same image can be deployed against a mock backend in CI and a real GPU in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, FrozenSet, Optional

ENV_PREFIX = "LLMSERVE_"
_TRUTHY = {"1", "true", "t", "yes", "y", "on"}
_FALSY = {"0", "false", "f", "no", "n", "off"}


def _raw(name: str) -> Optional[str]:
    value = os.environ.get(ENV_PREFIX + name.upper())
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_bool(value: str, name: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ValueError(f"{ENV_PREFIX}{name.upper()}: expected a boolean, got {value!r}")


def _as_keys(value: str) -> FrozenSet[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    # --- backend -----------------------------------------------------------
    backend: str = "mock"
    model: str = "facebook/opt-125m"
    served_model_name: Optional[str] = None

    # --- server ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000

    # --- admission control -------------------------------------------------
    max_concurrent_requests: int = 8
    max_queue_size: int = 64
    queue_timeout_s: float = 15.0
    request_timeout_s: float = 600.0
    drain_timeout_s: float = 30.0

    # --- request limits ----------------------------------------------------
    max_tokens_cap: int = 2048
    max_prompt_chars: int = 64_000

    # --- streaming ---------------------------------------------------------
    stream_keepalive_s: float = 15.0

    # --- auth / observability ----------------------------------------------
    api_keys: FrozenSet[str] = frozenset()
    log_level: str = "INFO"
    log_json: bool = True
    enable_metrics: bool = True

    # --- mock backend shape ------------------------------------------------
    mock_ttft_ms: float = 120.0
    mock_itl_ms: float = 18.0
    mock_jitter: float = 0.25
    mock_contention: float = 0.06
    mock_seed: int = 1234

    # --- vLLM engine args --------------------------------------------------
    vllm_tensor_parallel_size: int = 1
    vllm_gpu_memory_utilization: float = 0.90
    vllm_max_model_len: Optional[int] = None
    vllm_dtype: str = "auto"
    vllm_enforce_eager: bool = False
    vllm_trust_remote_code: bool = False
    vllm_swap_space_gb: int = 4

    @property
    def model_id(self) -> str:
        """Name reported to clients in API responses."""
        return self.served_model_name or self.model

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            raw = _raw(f.name)
            if raw is None:
                continue
            kwargs[f.name] = _coerce(f.name, f.type, raw)
        settings = cls(**kwargs)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.backend not in {"mock", "vllm"}:
            raise ValueError(f"backend must be 'mock' or 'vllm', got {self.backend!r}")
        if self.max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1")
        if self.max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")
        if self.queue_timeout_s <= 0:
            raise ValueError("queue_timeout_s must be > 0")
        if self.max_tokens_cap < 1:
            raise ValueError("max_tokens_cap must be >= 1")

    def describe(self) -> dict[str, Any]:
        """Loggable view of the config with secrets redacted."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "api_keys":
                out[f.name] = f"<{len(value)} key(s)>"
            elif isinstance(value, frozenset):
                out[f.name] = sorted(value)
            else:
                out[f.name] = value
        return out


def _coerce(name: str, type_: Any, raw: str) -> Any:
    """Coerce an env string using the declared field type (string annotations included)."""
    hint = type_ if isinstance(type_, str) else getattr(type_, "__name__", str(type_))
    if "FrozenSet" in hint or "frozenset" in hint:
        return _as_keys(raw)
    if "bool" in hint:
        return _as_bool(raw, name)
    if "int" in hint:
        return int(raw)
    if "float" in hint:
        return float(raw)
    return raw


def get_settings() -> Settings:
    """Read settings from the process environment (call once at startup)."""
    return Settings.from_env()
