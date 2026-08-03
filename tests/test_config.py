"""Tests for cwd-independent application configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DATABASE_PATH,
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
