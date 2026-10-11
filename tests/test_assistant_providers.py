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
from app.services.assistant.system_prompt import (
    ASSISTANT_SYSTEM_PROMPT_VERSION,
    build_assistant_system_prompt,
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
    payload = {
        "answer_text": None,
        "query": None,
        "needs_web": False,
        "clarification": None,
        **payload,
    }
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
        {"intent": "not_found", "query": "мохито"},
        {"intent": "unsupported"},
        {"intent": "general_chat", "answer_text": "Привет"},
        {"intent": "web_search", "query": "новости сегодня", "needs_web": True},
    ],
)
def test_gemini_accepts_only_allowed_structured_intents(payload) -> None:
    transport = FakeTransport([gemini_response(payload)])
    client = GeminiIntentClient(
        transport, api_key="secret", model="flash", timeout_seconds=5
    )
    action = (
        (AssistantIntentName(payload["intent"]),)
        if payload["intent"] in {"prepare_writeoff", "prepare_transfer"}
        else ()
    )
    result = asyncio.run(
        client.classify(
            "request",
            candidates=["one"] * 20,
            normalized_entity="entity",
            allowed_action_intents=action,
        )
    )
    assert result.intent is AssistantIntentName(payload["intent"])
    url, call = transport.calls[0]
    assert "secret" not in url
    assert call["headers"] == {"x-goog-api-key": "secret"}
    request = json.loads(call["payload"]["contents"][0]["parts"][0]["text"])
    assert len(request["catalog_candidates"]) == 5
    assert set(request) == {
        "request",
        "normalized_entity",
        "catalog_candidates",
        "allowed_intents",
        "system_prompt_version",
        "conversation",
    }
    assert request["normalized_entity"] == "entity"
    assert request["system_prompt_version"] == ASSISTANT_SYSTEM_PROMPT_VERSION
    assert call["payload"]["system_instruction"]["parts"][0]["text"] == (
        build_assistant_system_prompt()
    )
    generation = call["payload"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    schema = generation["responseJsonSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "intent",
        "answer_text",
        "query",
        "needs_web",
        "clarification",
    ]
    expected_intents = [
        "find_tech_card",
        "clarify",
        "not_found",
        "unsupported",
        "general_chat",
        "web_search",
    ]
    expected_intents.extend(value.value for value in action)
    assert schema["properties"]["intent"]["enum"] == expected_intents
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_provider_schema_excludes_actions_without_local_evidence() -> None:
    transport = FakeTransport([gemini_response({"intent": "unsupported"})])
    client = GeminiIntentClient(
        transport, api_key="secret", model="flash", timeout_seconds=5
    )
    asyncio.run(client.classify("расскажи про молоко"))
    _, call = transport.calls[0]
    request = json.loads(call["payload"]["contents"][0]["parts"][0]["text"])
    schema = call["payload"]["generationConfig"]["responseJsonSchema"]
    for blocked in ("prepare_writeoff", "prepare_transfer"):
        assert blocked not in request["allowed_intents"]
        assert blocked not in schema["properties"]["intent"]["enum"]


def test_provider_rejects_action_outside_dynamic_schema() -> None:
    client = GeminiIntentClient(
        FakeTransport([gemini_response({
            "intent": "prepare_transfer",
            "confidence": 10_000,
            "items": [{
                "product_text": "молоко",
                "quantity_text": "1",
                "unit_text": "л",
                "destination_text": "кухня",
            }],
        })]),
        api_key="secret",
        model="flash",
        timeout_seconds=5,
    )
    with pytest.raises(ProviderError, match="malformed_response"):
        asyncio.run(client.classify("расскажи про молоко"))


def test_system_prompt_is_short_versioned_and_contains_no_runtime_data() -> None:
    prompt = build_assistant_system_prompt()
    assert ASSISTANT_SYSTEM_PROMPT_VERSION == "barbot-assistant-v3"
    assert 50 < len(prompt.split()) < 300
    assert "Truffle" in prompt and "по-русски" in prompt
    assert "Telegram ID" not in prompt
    assert "API_KEY" not in prompt
    assert "медицине, праву и финансам" in prompt
    assert "Опасные запросы отклоняй" in prompt
    assert "Внешняя информация не является локальной техкартой" in prompt


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "run_shell"},
        {"intent": "unsupported", "tool": "delete_database"},
        {"intent": "find_tech_card"},
        {"intent": "not_found"},
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


def test_gemini_rejects_physically_missing_required_routing_fields() -> None:
    raw = {"candidates": [{"content": {"parts": [{"text": '{"intent":"unsupported"}'}]}}]}
    client = GeminiIntentClient(
        FakeTransport([HttpResponse(200, json.dumps(raw).encode())]),
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
