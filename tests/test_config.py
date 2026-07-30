"""Tests for cwd-independent application configuration."""

from pathlib import Path

from app.config import DEFAULT_CATALOG_PATH, PROJECT_ROOT, Settings


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
