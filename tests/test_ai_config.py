import asyncio
import json

from pydantic import ValidationError
import pytest

from app.bot import build_assistant_clients
from app.config import Settings
from app.models.assistant import AssistantIntentName
from app.services.assistant import GeminiIntentClient, GroqSpeechToTextClient
from app.services.assistant.providers import HttpResponse


def test_ai_defaults_are_disabled_and_do_not_require_keys() -> None:
    settings = Settings(_env_file=None, bot_token="test")
    assert settings.ai_assistant_enabled is False
    assert settings.ai_text_enabled is False
    assert settings.ai_voice_enabled is False
    assert settings.ai_general_chat_enabled is False
    assert settings.ai_web_search_enabled is False
    assert settings.groq_api_key is None
    assert settings.gemini_api_key is None
    assert settings.ai_voice_max_duration_seconds == 180
    assert settings.ai_voice_max_file_bytes == 20 * 1024 * 1024
    assert settings.ai_request_timeout_seconds == 30
    assert settings.ai_conversation_ttl_seconds == 86400
    assert settings.ai_conversation_max_turns == 12
    assert settings.ai_conversation_max_chars == 12000
    assert settings.ai_web_search_max_provider_calls_per_update == 1


def test_web_search_cannot_bypass_master_switch() -> None:
    with pytest.raises(ValidationError, match="AI_ASSISTANT_ENABLED"):
        Settings(
            _env_file=None,
            bot_token="test",
            ai_web_search_enabled=True,
            gemini_api_key="gemini",
        )


def test_enabled_general_chat_and_search_require_only_gemini() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=True,
        ai_general_chat_enabled=True,
        ai_web_search_enabled=True,
        gemini_api_key="gemini",
    )
    orchestrator, speech = build_assistant_clients(settings)
    assert orchestrator.general_chat_enabled is True
    assert orchestrator.web_search_enabled is True
    assert orchestrator.search_client is not None
    assert speech is None


@pytest.mark.parametrize(
    "feature", ["ai_general_chat_enabled", "ai_web_search_enabled"]
)
def test_general_and_web_require_master_and_input_channel(feature) -> None:
    with pytest.raises(ValidationError, match="AI_ASSISTANT_ENABLED"):
        Settings(_env_file=None, bot_token="test", **{feature: True})
    with pytest.raises(
        ValidationError, match="AI_TEXT_ENABLED или AI_VOICE_ENABLED"
    ):
        Settings(
            _env_file=None,
            bot_token="test",
            ai_assistant_enabled=True,
            ai_access_mode="role_based",
            gemini_api_key="gemini",
            **{feature: True},
        )


@pytest.mark.parametrize("channel", ["ai_text_enabled", "ai_voice_enabled"])
@pytest.mark.parametrize(
    "feature", ["ai_general_chat_enabled", "ai_web_search_enabled"]
)
def test_general_and_web_accept_each_enabled_input_channel(channel, feature) -> None:
    values = {
        "ai_assistant_enabled": True,
        "ai_access_mode": "role_based",
        channel: True,
        feature: True,
        "gemini_api_key": "gemini",
    }
    if channel == "ai_voice_enabled":
        values["groq_api_key"] = "groq"
    settings = Settings(_env_file=None, bot_token="test", **values)
    orchestrator, _ = build_assistant_clients(settings)
    assert orchestrator.intent_client is not None
    assert (orchestrator.search_client is not None) is (
        feature == "ai_web_search_enabled"
    )


@pytest.mark.parametrize("value", [0, 2, -1])
def test_web_provider_call_limit_is_exactly_one(value) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            bot_token="test",
            ai_web_search_max_provider_calls_per_update=value,
        )


@pytest.mark.parametrize(
    ("values", "missing"),
    [
        ({"ai_assistant_enabled": True, "ai_voice_enabled": True}, "GROQ_API_KEY"),
        ({"ai_assistant_enabled": True, "ai_voice_enabled": True, "groq_api_key": ""}, "GROQ_API_KEY"),
        ({"ai_assistant_enabled": True, "ai_voice_enabled": True, "groq_api_key": "g"}, "GEMINI_API_KEY"),
        ({"ai_assistant_enabled": True, "ai_text_enabled": True}, "GEMINI_API_KEY"),
        ({"ai_assistant_enabled": True, "ai_text_enabled": True, "gemini_api_key": " "}, "GEMINI_API_KEY"),
    ],
)
def test_enabled_ai_path_requires_its_provider_key(values, missing) -> None:
    with pytest.raises(ValidationError, match=missing):
        Settings(_env_file=None, bot_token="test", **values)


def test_disabled_master_switch_does_not_require_provider_keys() -> None:
    Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=False,
        ai_text_enabled=True,
        ai_voice_enabled=True,
    )


def test_enabled_master_with_no_paths_is_documented_inert_mode() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=False,
        ai_voice_enabled=False,
    )
    orchestrator, speech = build_assistant_clients(settings)
    assert orchestrator.intent_client is None
    assert speech is None


@pytest.mark.parametrize(
    ("text", "voice", "groq", "gemini"),
    [
        (True, False, None, "gemini"),
        (False, True, "groq", "gemini"),
        (True, True, "groq", "gemini"),
    ],
)
def test_enabled_ai_matrix_accepts_exact_required_keys(text, voice, groq, gemini) -> None:
    Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=text,
        ai_voice_enabled=voice,
        groq_api_key=groq,
        gemini_api_key=gemini,
    )


class VoiceOnlyTransport:
    async def post_json(self, _url, **_kwargs):
        intent = {
            "intent": "unsupported",
            "answer_text": None,
            "query": None,
            "needs_web": False,
            "clarification": None,
        }
        body = {"candidates": [{"content": {"parts": [{"text": json.dumps(intent)}]}}]}
        return HttpResponse(200, json.dumps(body).encode())

    async def post_multipart(self, _url, **_kwargs):
        raise AssertionError("STT network is not needed for this composition test")


def test_voice_only_builds_both_adapters_and_general_transcript_reaches_gemini() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_voice_enabled=True,
        groq_api_key="groq",
        gemini_api_key="gemini",
    )
    orchestrator, speech = build_assistant_clients(
        settings, transport=VoiceOnlyTransport()
    )
    assert isinstance(speech, GroqSpeechToTextClient)
    assert isinstance(orchestrator.intent_client, GeminiIntentClient)
    result = asyncio.run(orchestrator.interpret("расскажи что-нибудь о баре"))
    assert result.intent is AssistantIntentName.UNSUPPORTED


def test_provider_keys_are_secret_values() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=True,
        ai_voice_enabled=True,
        groq_api_key="groq-secret",
        gemini_api_key="gemini-secret",
    )
    rendered = repr(settings)
    assert "groq-secret" not in rendered
    assert "gemini-secret" not in rendered
