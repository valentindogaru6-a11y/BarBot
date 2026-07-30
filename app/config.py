"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Validated runtime settings for the Telegram bot."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: SecretStr
    catalog_path: Path = DEFAULT_CATALOG_PATH

    @field_validator("catalog_path")
    @classmethod
    def resolve_catalog_path(cls, path: Path) -> Path:
        """Resolve relative catalog paths from the repository root."""

        if path.is_absolute():
            return path
        return PROJECT_ROOT / path
