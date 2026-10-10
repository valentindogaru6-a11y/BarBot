"""Small REST adapters for Groq transcription and Gemini intent parsing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import aiohttp
from pydantic import ValidationError

from app.models.assistant import AssistantIntent
from app.services.assistant.system_prompt import (
    ASSISTANT_SYSTEM_PROMPT_VERSION,
    build_assistant_system_prompt,
)

MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
GROQ_TRANSCRIPTION_URL = (
    "https://api.groq.com/openai/v1/audio/transcriptions"
)
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_INTENT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "find_tech_card",
                "prepare_writeoff",
                "prepare_transfer",
                "clarify",
                "not_found",
                "unsupported",
            ],
        },
        "confidence": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "items": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_text": {"type": ["string", "null"]},
                    "quantity_text": {"type": ["string", "null"]},
                    "unit_text": {"type": ["string", "null"]},
                    "destination_text": {"type": ["string", "null"]},
                    "comment_text": {"type": ["string", "null"]},
                },
            },
        },
        "query": {"type": ["string", "null"]},
        "clarification": {"type": ["string", "null"]},
    },
    "required": ["intent"],
}


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"


class ProviderError(RuntimeError):
    """Sanitized provider failure without credentials or response bodies."""

    def __init__(self, provider: str, kind: ProviderErrorKind) -> None:
        super().__init__(f"{provider} request failed: {kind.value}")
        self.provider = provider
        self.kind = kind


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class AssistantHttpTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> HttpResponse: ...

    async def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        file_name: str,
        content_type: str,
        file_bytes: bytes,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class _ResponseTooLargeError(RuntimeError):
    pass


class AioHttpTransport:
    """One-request sessions avoid shared lifecycle and close deterministically."""

    @staticmethod
    async def _read_limited(
        response: aiohttp.ClientResponse,
        limit: int,
    ) -> bytes:
        body = await response.content.read(limit + 1)
        if len(body) > limit:
            raise _ResponseTooLargeError
        return body

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> HttpResponse:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                body = await self._read_limited(response, max_response_bytes)
                return HttpResponse(response.status, body)

    async def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        file_name: str,
        content_type: str,
        file_bytes: bytes,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> HttpResponse:
        form = aiohttp.FormData()
        for name, value in fields.items():
            form.add_field(name, value)
        form.add_field(
            "file",
            file_bytes,
            filename=file_name,
            content_type=content_type,
        )
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=form) as response:
                body = await self._read_limited(response, max_response_bytes)
                return HttpResponse(response.status, body)


class SpeechToTextClient(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        vocabulary: Sequence[str] = (),
    ) -> str: ...


class IntentModelClient(Protocol):
    async def classify(
        self,
        text: str,
        *,
        candidates: Sequence[str] = (),
        normalized_entity: str = "",
    ) -> AssistantIntent: ...


class _RetryingProvider:
    provider_name: str

    def __init__(
        self,
        *,
        timeout_seconds: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep
        self.retry_delay_seconds = retry_delay_seconds

    @staticmethod
    def _status_kind(status: int) -> ProviderErrorKind | None:
        if status in {401, 403}:
            return ProviderErrorKind.AUTH
        if status == 429:
            return ProviderErrorKind.RATE_LIMIT
        if status >= 500:
            return ProviderErrorKind.SERVER
        if status >= 400:
            return ProviderErrorKind.UNAVAILABLE
        return None

    async def _request_with_retry(
        self,
        request: Callable[[], Awaitable[HttpResponse]],
    ) -> HttpResponse:
        for attempt in range(2):
            try:
                response = await request()
            except _ResponseTooLargeError as exc:
                raise ProviderError(
                    self.provider_name, ProviderErrorKind.RESPONSE_TOO_LARGE
                ) from exc
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise ProviderError(
                    self.provider_name, ProviderErrorKind.TIMEOUT
                ) from exc
            except aiohttp.ClientError as exc:
                raise ProviderError(
                    self.provider_name, ProviderErrorKind.UNAVAILABLE
                ) from exc
            kind = self._status_kind(response.status)
            if kind is None:
                return response
            if kind in {ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.SERVER} and attempt == 0:
                await self._sleep(self.retry_delay_seconds)
                continue
            raise ProviderError(self.provider_name, kind)
        raise AssertionError("unreachable")


class GroqSpeechToTextClient(_RetryingProvider):
    provider_name = "groq"

    def __init__(
        self,
        transport: AssistantHttpTransport,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            retry_delay_seconds=retry_delay_seconds,
        )
        self.transport = transport
        self._api_key = api_key
        self.model = model

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        vocabulary: Sequence[str] = (),
    ) -> str:
        if not audio:
            raise ProviderError("groq", ProviderErrorKind.MALFORMED_RESPONSE)
        terms = ["списание", "перемещение", "проработка", "Бар", "Кухня", "Кондитерка", "кг", "грамм", "литр", "миллилитр", "штука"]
        terms.extend(value.strip()[:100] for value in vocabulary[:20] if value.strip())
        prompt = ", ".join(terms)[:1000]

        async def request() -> HttpResponse:
            return await self.transport.post_multipart(
                GROQ_TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                fields={
                    "model": self.model,
                    "language": "ru",
                    "prompt": prompt,
                    "response_format": "json",
                },
                file_name="voice.ogg",
                content_type=content_type,
                file_bytes=audio,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            )

        response = await self._request_with_retry(request)
        try:
            payload = json.loads(response.body)
            transcript = payload["text"].strip()
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "groq", ProviderErrorKind.MALFORMED_RESPONSE
            ) from exc
        if not transcript or len(transcript) > 4_000:
            raise ProviderError("groq", ProviderErrorKind.MALFORMED_RESPONSE)
        return transcript


class GeminiIntentClient(_RetryingProvider):
    provider_name = "gemini"

    def __init__(
        self,
        transport: AssistantHttpTransport,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            retry_delay_seconds=retry_delay_seconds,
        )
        self.transport = transport
        self._api_key = api_key
        self.model = model

    async def classify(
        self,
        text: str,
        *,
        candidates: Sequence[str] = (),
        normalized_entity: str = "",
    ) -> AssistantIntent:
        safe_candidates = [
            value.strip()[:200] for value in candidates[:5] if value.strip()
        ]
        request_data = {
            "request": text[:2_000],
            "normalized_entity": normalized_entity[:200],
            "catalog_candidates": safe_candidates,
            "allowed_intents": [
                "find_tech_card",
                "prepare_writeoff",
                "prepare_transfer",
                "clarify",
                "not_found",
                "unsupported",
            ],
            "system_prompt_version": ASSISTANT_SYSTEM_PROMPT_VERSION,
        }
        payload: Mapping[str, object] = {
            "system_instruction": {
                "parts": [{"text": build_assistant_system_prompt()}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": json.dumps(request_data, ensure_ascii=False)}]}
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": GEMINI_INTENT_SCHEMA,
                "maxOutputTokens": 1024,
            },
        }
        url = f"{GEMINI_API_ROOT}/{self.model}:generateContent"

        async def request() -> HttpResponse:
            return await self.transport.post_json(
                url,
                headers={"x-goog-api-key": self._api_key},
                payload=payload,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            )

        response = await self._request_with_retry(request)
        try:
            outer = json.loads(response.body)
            raw = outer["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw)
            return AssistantIntent.model_validate(parsed)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise ProviderError(
                "gemini", ProviderErrorKind.MALFORMED_RESPONSE
            ) from exc
