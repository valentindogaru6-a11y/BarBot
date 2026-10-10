from pydantic import ValidationError
import pytest

from app.config import Settings


def test_ai_defaults_are_disabled_and_do_not_require_keys() -> None:
    settings = Settings(_env_file=None, bot_token="test")
    assert settings.ai_assistant_enabled is False
    assert settings.ai_text_enabled is False
    assert settings.ai_voice_enabled is False
    assert settings.groq_api_key is None
    assert settings.gemini_api_key is None
    assert settings.ai_voice_max_duration_seconds == 180
    assert settings.ai_voice_max_file_bytes == 20 * 1024 * 1024
    assert settings.ai_request_timeout_seconds == 30


@pytest.mark.parametrize(
    ("values", "missing"),
    [
        ({"ai_assistant_enabled": True, "ai_voice_enabled": True}, "GROQ_API_KEY"),
        ({"ai_assistant_enabled": True, "ai_voice_enabled": True, "groq_api_key": ""}, "GROQ_API_KEY"),
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


def test_provider_keys_are_secret_values() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_text_enabled=True,
        ai_voice_enabled=True,
        groq_api_key="groq-secret",
        gemini_api_key="gemini-secret",
    )
    rendered = repr(settings)
    assert "groq-secret" not in rendered
    assert "gemini-secret" not in rendered
