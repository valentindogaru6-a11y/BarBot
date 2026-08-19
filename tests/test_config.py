"""Tests for cwd-independent application configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DATABASE_PATH,
    DEFAULT_VOSK_MODEL_PATH,
    PROJECT_ROOT,
    Settings,
)


def test_default_catalog_path_is_absolute_and_project_relative(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CATALOG_PATH", raising=False)

    settings = Settings(_env_file=None, bot_token="test-token")

    assert settings.catalog_path == DEFAULT_CATALOG_PATH
    assert settings.catalog_path == PROJECT_ROOT / "data" / "catalog.json"
    assert settings.catalog_path.is_absolute()


def test_relative_catalog_path_is_resolved_from_project_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CATALOG_PATH", raising=False)

    settings = Settings(
        _env_file=None,
        bot_token="test-token",
        catalog_path=Path("data/custom.json"),
    )

    assert settings.catalog_path == PROJECT_ROOT / "data" / "custom.json"


def test_parses_super_admin_json_array() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test-token",
        super_admin_ids="[123456789, 987654321]",
    )

    assert settings.effective_super_admin_ids == frozenset(
        {123456789, 987654321}
    )


def test_falls_back_to_deprecated_admin_ids() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test-token",
        admin_ids="11,22",
    )

    assert settings.super_admin_ids is None
    assert settings.effective_super_admin_ids == frozenset({11, 22})


@pytest.mark.parametrize(
    "value",
    ["[0]", "[-1]", "[true]", "[\"bad\"]", "not-json"],
)
def test_rejects_invalid_super_admin_ids(value: str) -> None:
    with pytest.raises(ValidationError, match="SUPER_ADMIN_IDS"):
        Settings(
            _env_file=None,
            bot_token="test-token",
            super_admin_ids=value,
        )


def test_database_path_is_project_relative() -> None:
    default = Settings(_env_file=None, bot_token="test-token")
    custom = Settings(
        _env_file=None,
        bot_token="test-token",
        database_path=Path("data/custom.db"),
    )

    assert default.database_path == DEFAULT_DATABASE_PATH
    assert custom.database_path == PROJECT_ROOT / "data" / "custom.db"


def test_voice_search_defaults_are_safe_and_project_relative() -> None:
    settings = Settings(_env_file=None, bot_token="test-token")

    assert settings.voice_search_enabled is True
    assert settings.vosk_model_path == DEFAULT_VOSK_MODEL_PATH
    assert settings.vosk_language == "ru"
    assert settings.voice_max_duration_seconds == 30
    assert settings.voice_max_file_size_bytes == 10 * 1024 * 1024
    assert settings.voice_max_concurrent == 1
    assert settings.voice_debug_transcripts is False


def test_relative_vosk_path_is_resolved_from_project_root() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="test-token",
        vosk_model_path=Path("models/custom-ru"),
    )

    assert settings.vosk_model_path == PROJECT_ROOT / "models" / "custom-ru"


def test_relative_vosk_path_cannot_escape_project() -> None:
    with pytest.raises(ValidationError, match="VOSK_MODEL_PATH"):
        Settings(
            _env_file=None,
            bot_token="test-token",
            vosk_model_path=Path("../outside-model"),
        )


def test_absolute_vosk_path_cannot_escape_project(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="VOSK_MODEL_PATH"):
        Settings(
            _env_file=None,
            bot_token="test-token",
            vosk_model_path=tmp_path / "outside-model",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("voice_max_duration_seconds", 0),
        ("voice_max_file_size_mb", 0),
        ("voice_max_concurrent", 0),
    ],
)
def test_voice_numeric_limits_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            bot_token="test-token",
            **{field: value},
        )
