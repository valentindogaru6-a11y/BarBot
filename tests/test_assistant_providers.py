import asyncio
import json

import pytest

from app.models.assistant import AssistantIntentName
from app.services.assistant.providers import (
    GeminiIntentClient,
    GroqSpeechToTextClient,
    HttpResponse,
    ProviderError,
    ProviderErrorKind,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def post_multipart(self, url, **kwargs):
        return await self.post_json(url, **kwargs)


async def no_sleep(_delay):
    return None


def gemini_response(payload: dict) -> HttpResponse:
    outer = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
    return HttpResponse(200, json.dumps(outer).encode())


def test_groq_transcribes_with_bounded_russian_request() -> None:
    transport = FakeTransport([HttpResponse(200, b'{"text":"  spishi moloko  "}')])
    client = GroqSpeechToTextClient(
        transport, api_key="secret", model="whisper", timeout_seconds=7
    )
    assert asyncio.run(client.transcribe(b"ogg", content_type="audio/ogg")) == "spishi moloko"
    url, call = transport.calls[0]
    assert "secret" not in url
    assert call["headers"] == {"Authorization": "Bearer secret"}
    assert call["fields"]["language"] == "ru"
    assert call["fields"]["model"] == "whisper"
    assert call["timeout_seconds"] == 7


@pytest.mark.parametrize(
    ("responses", "kind", "call_count"),
    [
        ([HttpResponse(401, b"")], ProviderErrorKind.AUTH, 1),
        ([HttpResponse(429, b""), HttpResponse(429, b"")], ProviderErrorKind.RATE_LIMIT, 2),
        ([HttpResponse(500, b""), HttpResponse(500, b"")], ProviderErrorKind.SERVER, 2),
        ([asyncio.TimeoutError()], ProviderErrorKind.TIMEOUT, 1),
        ([HttpResponse(200, b'{"text":""}')], ProviderErrorKind.MALFORMED_RESPONSE, 1),
    ],
)
def test_groq_returns_typed_sanitized_errors(responses, kind, call_count) -> None:
    transport = FakeTransport(responses)
    client = GroqSpeechToTextClient(
        transport, api_key="never-log-me", model="whisper", timeout_seconds=1, sleep=no_sleep
    )
    with pytest.raises(ProviderError) as caught:
        asyncio.run(client.transcribe(b"ogg", content_type="audio/ogg"))
    assert caught.value.kind is kind
    assert "never-log-me" not in str(caught.value)
    assert len(transport.calls) == call_count


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "find_tech_card", "query": "Kiss Me"},
        {"intent": "prepare_writeoff", "confidence": 9000, "items": [{"product_text": "морковь", "quantity_text": "0.5", "unit_text": "кг"}]},
        {"intent": "prepare_transfer", "confidence": 9000, "items": [{"product_text": "молоко", "quantity_text": "1", "unit_text": "л", "destination_text": "кухня"}]},
        {"intent": "clarify", "clarification": "Уточните количество"},
        {"intent": "unsupported"},
    ],
)
def test_gemini_accepts_only_allowed_structured_intents(payload) -> None:
    transport = FakeTransport([gemini_response(payload)])
    client = GeminiIntentClient(
        transport, api_key="secret", model="flash", timeout_seconds=5
    )
    result = asyncio.run(client.classify("request", candidates=["one"] * 20))
    assert result.intent is AssistantIntentName(payload["intent"])
    url, call = transport.calls[0]
    assert "secret" not in url
    assert call["headers"] == {"x-goog-api-key": "secret"}
    request = json.loads(call["payload"]["contents"][0]["parts"][0]["text"])
    assert len(request["catalog_candidates"]) == 8
    assert set(request) == {"request", "catalog_candidates", "allowed_intents"}
    generation = call["payload"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    schema = generation["responseJsonSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["intent"]
    assert schema["properties"]["intent"]["enum"] == [
        "find_tech_card",
        "prepare_writeoff",
        "prepare_transfer",
        "clarify",
        "unsupported",
    ]
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "run_shell"},
        {"intent": "unsupported", "tool": "delete_database"},
        {"intent": "find_tech_card"},
    ],
)
def test_gemini_rejects_unknown_extra_or_incomplete_output(payload) -> None:
    client = GeminiIntentClient(
        FakeTransport([gemini_response(payload)]),
        api_key="secret",
        model="flash",
        timeout_seconds=5,
    )
    with pytest.raises(ProviderError, match="malformed_response"):
        asyncio.run(client.classify("request"))


@pytest.mark.parametrize(
    ("responses", "kind", "call_count"),
    [
        ([HttpResponse(401, b"private raw auth body")], ProviderErrorKind.AUTH, 1),
        ([HttpResponse(429, b"private raw rate body"), HttpResponse(429, b"still private")], ProviderErrorKind.RATE_LIMIT, 2),
        ([asyncio.TimeoutError()], ProviderErrorKind.TIMEOUT, 1),
        ([HttpResponse(500, b"temporary"), gemini_response({"intent": "unsupported"})], None, 2),
        ([HttpResponse(503, b"private raw server body"), HttpResponse(503, b"still private")], ProviderErrorKind.SERVER, 2),
        ([HttpResponse(200, b"not-json-private-body")], ProviderErrorKind.MALFORMED_RESPONSE, 1),
    ],
)
def test_gemini_errors_and_retry_are_bounded_and_sanitized(
    responses, kind, call_count, caplog
) -> None:
    transport = FakeTransport(responses)
    client = GeminiIntentClient(
        transport,
        api_key="gemini-never-log-me",
        model="flash",
        timeout_seconds=1,
        sleep=no_sleep,
    )
    if kind is None:
        result = asyncio.run(client.classify("request"))
        assert result.intent is AssistantIntentName.UNSUPPORTED
    else:
        with pytest.raises(ProviderError) as caught:
            asyncio.run(client.classify("request"))
        assert caught.value.kind is kind
        rendered = str(caught.value) + caplog.text
        assert "gemini-never-log-me" not in rendered
        assert "private raw" not in rendered
        assert "not-json-private-body" not in rendered
    assert len(transport.calls) == call_count
