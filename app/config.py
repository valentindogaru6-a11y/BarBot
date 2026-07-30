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
    admin_ids: frozenset[int] = frozenset()

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> frozenset[int]:
        """Parse a comma-separated list of positive Telegram user IDs."""

        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if all(not part for part in parts):
                return frozenset()
            values: object = parts
        else:
            values = value

        if not isinstance(values, (list, tuple, set, frozenset)):
            raise ValueError(
                "ADMIN_IDS должен быть списком положительных числовых ID"
            )

        parsed: set[int] = set()
        for raw_id in values:
            try:
                user_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"ADMIN_IDS содержит нечисловое значение: {raw_id!r}"
                ) from exc
            if user_id <= 0:
                raise ValueError(
                    "ADMIN_IDS должен содержать только положительные ID"
                )
            parsed.add(user_id)
        return frozenset(parsed)

    @field_validator("catalog_path")
    @classmethod
    def resolve_catalog_path(cls, path: Path) -> Path:
        """Resolve relative catalog paths from the repository root."""

        if path.is_absolute():
            return path
        return PROJECT_ROOT / path
