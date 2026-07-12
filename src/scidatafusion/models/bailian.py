"""Minimal, auditable Bailian OpenAI-compatible structured-output client."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelUsage,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.errors import AppError, ErrorCode


class ModelCache(Protocol):
    def get(self, key: str) -> StructuredModelCompletion | None:
        """Return a prior completion for the request hash."""

    def put(self, key: str, value: StructuredModelCompletion) -> None:
        """Store a validated completion."""


class InMemoryModelCache:
    """Process-local cache used by development and deterministic tests."""

    def __init__(self) -> None:
        self._values: dict[str, StructuredModelCompletion] = {}

    def get(self, key: str) -> StructuredModelCompletion | None:
        return self._values.get(key)

    def put(self, key: str, value: StructuredModelCompletion) -> None:
        self._values[key] = value


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    content: str = Field(min_length=1)


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    message: _Message


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class _ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    model: str = Field(min_length=1)
    choices: tuple[_Choice, ...] = Field(min_length=1)
    usage: _Usage = Field(default_factory=_Usage)


Sleeper = Callable[[float], Awaitable[None]]


class BailianStructuredClient:
    """Call Qwen via Bailian with bounded concurrency, retry, cache, and audit records."""

    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache: ModelCache | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache or InMemoryModelCache()
        self._sleeper = sleeper
        self._semaphore = asyncio.Semaphore(settings.model_max_concurrency)

    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
        """Return a validated completion or a structured external-service failure."""

        request_hash = self._request_hash(request)
        cached = self._cache.get(request_hash)
        if cached is not None:
            return cached.model_copy(
                update={"invocation": cached.invocation.model_copy(update={"cached": True})}
            )
        self._validate_runtime()
        async with self._semaphore:
            completion = await self._request_with_retry(request, request_hash)
        self._cache.put(request_hash, completion)
        return completion

    def _validate_runtime(self) -> None:
        if self._settings.offline_mode:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "Bailian calls are disabled while offline mode is enabled",
            )
        if (
            self._settings.dashscope_api_key is None
            or self._settings.resolved_qwen_base_url is None
        ):
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "Bailian credentials or endpoint missing")

    async def _request_with_retry(
        self, request: StructuredModelRequest, request_hash: str
    ) -> StructuredModelCompletion:
        last_error = "unknown error"
        for attempt in range(1, self._settings.model_max_retries + 2):
            started = perf_counter()
            try:
                response = await self._post(request)
                if response.status_code in self._RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise AppError(
                        ErrorCode.EXTERNAL_SERVICE_ERROR,
                        f"Bailian returned HTTP {response.status_code}",
                        retryable=False,
                    )
                else:
                    return self._parse_response(
                        request,
                        request_hash,
                        response,
                        attempt=attempt,
                        latency_ms=(perf_counter() - started) * 1000,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = type(exc).__name__
            if attempt <= self._settings.model_max_retries:
                await self._sleeper(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise AppError(
            ErrorCode.EXTERNAL_SERVICE_ERROR,
            f"Bailian request failed after retries: {last_error}",
            retryable=True,
        )

    async def _post(self, request: StructuredModelRequest) -> httpx.Response:
        base_url = self._settings.resolved_qwen_base_url
        key = self._settings.dashscope_api_key
        if base_url is None or key is None:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "Bailian configuration incomplete")
        payload = {
            "model": request.model_id,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
        if self._client is not None:
            return await self._client.post(
                f"{base_url}/chat/completions", json=payload, headers=headers
            )
        timeout = httpx.Timeout(self._settings.model_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)

    def _parse_response(
        self,
        request: StructuredModelRequest,
        request_hash: str,
        response: httpx.Response,
        *,
        attempt: int,
        latency_ms: float,
    ) -> StructuredModelCompletion:
        try:
            parsed = _ChatResponse.model_validate_json(response.content)
        except (ValueError, ValidationError) as exc:
            raise AppError(
                ErrorCode.EXTERNAL_SERVICE_ERROR,
                "Bailian returned an invalid response contract",
                retryable=False,
            ) from exc
        content = parsed.choices[0].message.content
        response_hash = hashlib.sha256(response.content).hexdigest()
        host = urlparse(self._settings.resolved_qwen_base_url or "").hostname or "unknown"
        invocation = ModelInvocationRecord(
            region=self._settings.bailian_region.value,
            endpoint_host=host,
            requested_model=request.model_id,
            actual_model=parsed.model,
            role=request.role,
            prompt_version=request.prompt_version,
            schema_name=request.schema_name,
            request_hash=request_hash,
            response_hash=response_hash,
            usage=ModelUsage(
                input_tokens=parsed.usage.prompt_tokens,
                output_tokens=parsed.usage.completion_tokens,
            ),
            latency_ms=latency_ms,
            attempt_count=attempt,
        )
        return StructuredModelCompletion(content=content, invocation=invocation)

    @staticmethod
    def _request_hash(request: StructuredModelRequest) -> str:
        encoded = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()
