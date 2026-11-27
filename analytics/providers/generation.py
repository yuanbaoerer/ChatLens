"""Typed generation providers for opt-in, bounded OpenAI-compatible calls.

The module separates the provider contract from analyzer/report logic. It never
logs prompts, responses, endpoints with credentials, or API keys. The real
provider is disabled unless ``enabled=True`` is supplied by the caller.
"""
from __future__ import annotations

import json
import math
import os
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from analytics.analyzer import Cancellation
from analytics.providers.schema import SchemaValidationError, validate_json_schema

DEFAULT_API_KEY_ENV = "CHATLENS_GENERATION_API_KEY"
DEFAULT_MAX_RESPONSE_BYTES = 4_000_000


class GenerationError(RuntimeError):
    """Base class for generation provider failures."""


class GenerationConfigurationError(GenerationError):
    """Raised when provider configuration is invalid."""


class ProviderDisabled(GenerationError):
    """Raised when a real provider is used without explicit opt-in."""


class GenerationBudgetExceeded(GenerationError):
    """Raised when a call or token budget would be exceeded."""


class GenerationSchemaError(GenerationError):
    """Raised when model output is not valid structured JSON for the schema."""


class GenerationTimeout(GenerationError):
    """Raised when a provider call exceeds its timeout."""


class GenerationNetworkError(GenerationError):
    """Raised when the provider cannot be reached."""


class GenerationHTTPError(GenerationError):
    """Raised when the provider returns an HTTP error."""

    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"generation endpoint returned HTTP {self.status}")


class GenerationResponseError(GenerationError):
    """Raised when the provider response envelope is malformed."""


def estimate_tokens(text: str) -> int:
    """Conservative dependency-free token estimate for preflight budgeting."""
    return max(1, math.ceil(len(str(text or "")) / 4))


@dataclass(frozen=True)
class GenerationMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in ("system", "user", "assistant"):
            raise ValueError("message role must be system, user or assistant")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("message content must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class GenerationRequest:
    messages: tuple[GenerationMessage, ...]
    response_schema: Mapping[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages or any(not isinstance(item, GenerationMessage) for item in self.messages):
            raise ValueError("messages must contain GenerationMessage values")
        if self.response_schema is not None:
            if not isinstance(self.response_schema, Mapping):
                raise TypeError("response_schema must be a mapping or None")
            object.__setattr__(self, "response_schema", MappingProxyType(dict(self.response_schema)))
        if self.max_output_tokens is not None:
            if (
                isinstance(self.max_output_tokens, bool)
                or not isinstance(self.max_output_tokens, int)
                or self.max_output_tokens < 1
            ):
                raise ValueError("max_output_tokens must be a positive integer or None")
        if self.temperature is not None:
            if (
                isinstance(self.temperature, bool)
                or not isinstance(self.temperature, (int, float))
                or not 0.0 <= float(self.temperature) <= 2.0
            ):
                raise ValueError("temperature must be between 0 and 2 or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def estimated_input_tokens(self) -> int:
        return sum(estimate_tokens(message.content) + 4 for message in self.messages)

    def provider_messages(self) -> list[dict[str, str]]:
        return [message.to_dict() for message in self.messages]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class GenerationResponse:
    output: Mapping[str, Any]
    provider_id: str
    model_id: str
    usage: TokenUsage
    finish_reason: str | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.output, Mapping):
            raise TypeError("generation output must be a mapping")
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": dict(self.output),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "finish_reason": self.finish_reason,
            "diagnostics": list(self.diagnostics),
        }


class GenerationBudget:
    """Thread-safe call/token budget shared by provider calls in one run."""

    def __init__(
        self,
        *,
        max_calls: int | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
    ) -> None:
        for name, value in (
            ("max_calls", max_calls),
            ("max_input_tokens", max_input_tokens),
            ("max_output_tokens", max_output_tokens),
            ("max_total_tokens", max_total_tokens),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer or None")
        self.max_calls = max_calls
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_total_tokens = max_total_tokens
        self._lock = threading.RLock()
        self._calls = 0
        self._input_tokens = 0
        self._output_tokens = 0

    def begin_call(self, estimated_input_tokens: int, requested_output_tokens: int = 0) -> None:
        if (
            isinstance(estimated_input_tokens, bool)
            or not isinstance(estimated_input_tokens, int)
            or estimated_input_tokens < 0
        ):
            raise ValueError("estimated_input_tokens must be a non-negative integer")
        if (
            isinstance(requested_output_tokens, bool)
            or not isinstance(requested_output_tokens, int)
            or requested_output_tokens < 0
        ):
            raise ValueError("requested_output_tokens must be a non-negative integer")
        with self._lock:
            if self.max_calls is not None and self._calls + 1 > self.max_calls:
                raise GenerationBudgetExceeded("generation call budget exceeded")
            if (
                self.max_input_tokens is not None
                and self._input_tokens + estimated_input_tokens > self.max_input_tokens
            ):
                raise GenerationBudgetExceeded("generation input token budget exceeded")
            if (
                self.max_output_tokens is not None
                and self._output_tokens + requested_output_tokens > self.max_output_tokens
            ):
                raise GenerationBudgetExceeded("generation output token budget exceeded")
            if self.max_total_tokens is not None and (
                self._input_tokens + self._output_tokens + estimated_input_tokens + requested_output_tokens
                > self.max_total_tokens
            ):
                raise GenerationBudgetExceeded("generation total token budget exceeded")
            self._calls += 1

    def record(self, usage: TokenUsage) -> None:
        if not isinstance(usage, TokenUsage):
            raise TypeError("usage must be TokenUsage")
        with self._lock:
            self._input_tokens += usage.input_tokens
            self._output_tokens += usage.output_tokens
            if self.max_input_tokens is not None and self._input_tokens > self.max_input_tokens:
                raise GenerationBudgetExceeded("generation input token budget exceeded")
            if self.max_output_tokens is not None and self._output_tokens > self.max_output_tokens:
                raise GenerationBudgetExceeded("generation output token budget exceeded")
            if self.max_total_tokens is not None and self._input_tokens + self._output_tokens > self.max_total_tokens:
                raise GenerationBudgetExceeded("generation total token budget exceeded")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self._calls,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._input_tokens + self._output_tokens,
            }


@runtime_checkable
class GenerationProvider(Protocol):
    provider_id: str
    model_id: str
    version: str

    def generate(
        self,
        request: GenerationRequest,
        cancellation: Cancellation | None = None,
    ) -> GenerationResponse:
        ...


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _validate_output(output: Any, schema: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(output, Mapping):
        raise GenerationSchemaError("generation output must be a JSON object")
    try:
        json.dumps(dict(output), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GenerationSchemaError("generation output is not JSON serializable") from error
    if schema is not None:
        try:
            validate_json_schema(output, schema)
        except SchemaValidationError as error:
            raise GenerationSchemaError(str(error)) from error
    return output


class MockGenerationProvider:
    """Deterministic, network-free provider for tests and CLI/no-cloud mode."""

    provider_id = "mock"
    version = "1"

    def __init__(
        self,
        outputs: Iterable[Mapping[str, Any]] | None = None,
        *,
        responder: Callable[[GenerationRequest], Mapping[str, Any]] | None = None,
        usage: TokenUsage | None = None,
        model_id: str = "mock-v1",
        budget: GenerationBudget | None = None,
    ) -> None:
        if outputs is not None and responder is not None:
            raise ValueError("provide outputs or responder, not both")
        self._outputs = list(outputs or ())
        if any(not isinstance(item, Mapping) for item in self._outputs):
            raise TypeError("outputs must contain mappings")
        self._responder = responder
        self._usage = usage or TokenUsage(1, 1)
        if not isinstance(self._usage, TokenUsage):
            raise TypeError("usage must be TokenUsage")
        self.model_id = str(model_id)
        self.budget = budget
        self._index = 0
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def generate(
        self,
        request: GenerationRequest,
        cancellation: Cancellation | None = None,
    ) -> GenerationResponse:
        if cancellation is not None:
            cancellation.check()
        if self.budget is not None:
            self.budget.begin_call(
                request.estimated_input_tokens(),
                request.max_output_tokens or 0,
            )
        with self._lock:
            self._calls += 1
            if self._responder is not None:
                output = self._responder(request)
            elif self._index < len(self._outputs):
                output = self._outputs[self._index]
                self._index += 1
            else:
                raise GenerationError("mock generation outputs were exhausted")
        if cancellation is not None:
            cancellation.check()
        mapping = _validate_output(output, request.response_schema)
        if self.budget is not None:
            self.budget.record(self._usage)
        return GenerationResponse(
            output=mapping,
            provider_id=self.provider_id,
            model_id=self.model_id,
            usage=self._usage,
            finish_reason="stop",
            diagnostics=("mock",),
        )


class OpenAICompatibleGenerationProvider:
    """Opt-in OpenAI-compatible chat-completions provider using stdlib HTTP."""

    provider_id = "openai-compatible"
    version = "1"

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        enabled: bool = False,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        timeout: float = 30.0,
        max_concurrency: int = 4,
        budget: GenerationBudget | None = None,
        json_mode: bool = True,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        user_agent: str = "ChatLens/analytics",
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise GenerationConfigurationError("endpoint must be a non-empty URL")
        endpoint = endpoint.strip()
        try:
            parsed = urlsplit(endpoint)
            scheme = parsed.scheme.lower()
            if scheme not in ("http", "https"):
                raise GenerationConfigurationError("endpoint must use http or https")
            if not parsed.hostname:
                raise GenerationConfigurationError("endpoint must include a host")
            if parsed.username is not None or parsed.password is not None:
                raise GenerationConfigurationError("endpoint must not contain credentials")
            if parsed.fragment:
                raise GenerationConfigurationError("endpoint must not contain a fragment")
            if scheme == "http" and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
                raise GenerationConfigurationError("remote generation endpoints must use https")
        except ValueError as error:
            raise GenerationConfigurationError("endpoint is not a valid URL") from error
        if not isinstance(model, str) or not model.strip():
            raise GenerationConfigurationError("model must be a non-empty string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise GenerationConfigurationError("timeout must be a positive finite number")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise GenerationConfigurationError("max_concurrency must be a positive integer")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1:
            raise GenerationConfigurationError("max_response_bytes must be a positive integer")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise GenerationConfigurationError("api_key_env must be a non-empty string")
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise GenerationConfigurationError("user_agent must be a non-empty string")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise GenerationConfigurationError("api_key must be a non-empty string or None")
        if budget is not None and not isinstance(budget, GenerationBudget):
            raise GenerationConfigurationError("budget must be a GenerationBudget or None")
        if not isinstance(enabled, bool):
            raise GenerationConfigurationError("enabled must be a boolean")
        self._endpoint = endpoint
        self.model_id = model.strip()
        self._enabled = enabled
        self._api_key = api_key.strip() if isinstance(api_key, str) else None
        self._api_key_env = api_key_env.strip()
        self._timeout = float(timeout)
        self._budget = budget
        self._json_mode = bool(json_mode)
        self._max_response_bytes = max_response_bytes
        self._user_agent = user_agent.strip()
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def __repr__(self) -> str:
        return f"OpenAICompatibleGenerationProvider(model={self.model_id!r}, enabled={self._enabled})"

    def _api_key_value(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        value = os.environ.get(self._api_key_env)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": request.provider_messages(),
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = float(request.temperature)
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if self._json_mode and request.response_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: Mapping[str, Any]) -> bytes:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        api_key = self._api_key_value()
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise GenerationHTTPError(error.code) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise GenerationTimeout("generation request timed out") from error
            raise GenerationNetworkError("generation endpoint is unavailable") from error
        except (TimeoutError, socket.timeout) as error:
            raise GenerationTimeout("generation request timed out") from error
        except OSError as error:
            raise GenerationNetworkError("generation endpoint is unavailable") from error
        if len(raw) > self._max_response_bytes:
            raise GenerationResponseError("generation response exceeded the size limit")
        return raw

    def _parse_envelope(
        self,
        raw: bytes,
        request: GenerationRequest,
    ) -> tuple[str, TokenUsage, bool, str | None, str]:
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise GenerationResponseError("generation response is not valid JSON") from error
        if not isinstance(body, Mapping):
            raise GenerationResponseError("generation response must be a JSON object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise GenerationResponseError("generation response has no choices")
        first = choices[0]
        message = first.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise GenerationResponseError("generation response has no message content")
        content = message["content"]
        usage = body.get("usage")
        usage_estimated = False
        input_tokens = 0
        output_tokens = 0
        if isinstance(usage, Mapping) and ("prompt_tokens" in usage or "completion_tokens" in usage):
            for key, target in (("prompt_tokens", "input"), ("completion_tokens", "output")):
                value = usage.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise GenerationResponseError("generation response has invalid usage")
                if target == "input":
                    input_tokens = value
                else:
                    output_tokens = value
        else:
            # Local OpenAI-compatible servers may omit usage. Account with a
            # conservative estimate rather than silently bypassing token budgets.
            input_tokens = request.estimated_input_tokens()
            output_tokens = estimate_tokens(content)
            usage_estimated = True
        finish_reason = first.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise GenerationResponseError("generation response has invalid finish_reason")
        model_id = body.get("model")
        if not isinstance(model_id, str) or not model_id:
            model_id = self.model_id
        return content, TokenUsage(input_tokens, output_tokens), usage_estimated, finish_reason, model_id

    @staticmethod
    def _parse_content(content: str) -> Mapping[str, Any]:
        try:
            output = json.loads(content, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise GenerationSchemaError("generation content is not valid JSON") from error
        if not isinstance(output, Mapping):
            raise GenerationSchemaError("generation content must be a JSON object")
        return output

    def generate(
        self,
        request: GenerationRequest,
        cancellation: Cancellation | None = None,
    ) -> GenerationResponse:
        if not self._enabled:
            raise ProviderDisabled("generation provider is disabled; explicit opt-in is required")
        if cancellation is not None:
            cancellation.check()
        if self._budget is not None:
            self._budget.begin_call(
                request.estimated_input_tokens(),
                request.max_output_tokens or 0,
            )
        with self._semaphore:
            if cancellation is not None:
                cancellation.check()
            raw = self._post(self._payload(request))
            content, usage, usage_estimated, finish_reason, model_id = self._parse_envelope(raw, request)
            if self._budget is not None:
                self._budget.record(usage)
            if cancellation is not None:
                cancellation.check()
            output = _validate_output(self._parse_content(content), request.response_schema)
            diagnostics = []
            if self._json_mode and request.response_schema:
                diagnostics.append("json_mode")
            if usage_estimated:
                diagnostics.append("usage_estimated")
            return GenerationResponse(
                output=output,
                provider_id=self.provider_id,
                model_id=model_id,
                usage=usage,
                finish_reason=finish_reason,
                diagnostics=tuple(diagnostics),
            )
