"""Application configuration loaded from environment variables."""

import json
import logging
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "barbot.db"
ENV_FILE = PROJECT_ROOT / ".env"
logger = logging.getLogger(__name__)


def _parse_positive_ids(value: object, field_name: str) -> frozenset[int] | None:
    if value is None or value == "":
        return None
    values: object = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("["):
            try:
                values = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{field_name} должен быть JSON-массивом положительных ID"
                ) from exc
        else:
            values = [part.strip() for part in stripped.split(",")]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError(
            f"{field_name} должен быть списком положительных числовых ID"
        )
    parsed: set[int] = set()
    for raw_id in values:
        if isinstance(raw_id, bool):
            raise ValueError(f"{field_name} содержит нечисловое значение")
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{field_name} содержит нечисловое значение"
            ) from exc
        if user_id <= 0:
            raise ValueError(
                f"{field_name} должен содержать только положительные ID"
            )
        parsed.add(user_id)
    return frozenset(parsed)


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
    database_path: Path = DEFAULT_DATABASE_PATH
    super_admin_ids: frozenset[int] | None = None
    admin_ids: frozenset[int] = frozenset()

    @field_validator("super_admin_ids", mode="before")
    @classmethod
    def parse_super_admin_ids(
        cls,
        value: object,
    ) -> frozenset[int] | None:
        """Parse the primary JSON-array super-administrator setting."""

        return _parse_positive_ids(value, "SUPER_ADMIN_IDS")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> frozenset[int]:
        """Parse the deprecated comma-separated administrator setting."""

        return _parse_positive_ids(value, "ADMIN_IDS") or frozenset()

    @field_validator("catalog_path", "database_path")
    @classmethod
    def resolve_project_path(cls, path: Path) -> Path:
        """Resolve relative data paths from the repository root."""

        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    @property
    def effective_super_admin_ids(self) -> frozenset[int]:
        """Return primary IDs or the deprecated ADMIN_IDS bootstrap."""

        if self.super_admin_ids is not None:
            return self.super_admin_ids
        if self.admin_ids:
            logger.warning(
                "ADMIN_IDS bootstrap is deprecated; configure SUPER_ADMIN_IDS"
            )
        return self.admin_ids
