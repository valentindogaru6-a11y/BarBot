"""Application configuration loaded from environment variables."""

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "barbot.db"
DEFAULT_VOSK_MODEL_PATH = PROJECT_ROOT / "models" / "vosk-model-small-ru-0.22"
DEFAULT_WAREHOUSE_REPORTS_PATH = PROJECT_ROOT / "reports" / "warehouse"
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
        hide_input_in_errors=True,
    )

    bot_token: SecretStr
    catalog_path: Path = DEFAULT_CATALOG_PATH
    database_path: Path = DEFAULT_DATABASE_PATH
    super_admin_ids: frozenset[int] | None = None
    admin_ids: frozenset[int] = frozenset()
    voice_search_enabled: bool = True
    vosk_model_path: Path = DEFAULT_VOSK_MODEL_PATH
    vosk_language: str = "ru"
    voice_max_duration_seconds: int = Field(default=30, gt=0)
    voice_max_file_size_mb: int = Field(default=10, gt=0)
    voice_max_concurrent: int = Field(default=1, gt=0)
    voice_debug_transcripts: bool = False
    warehouse_enabled: bool = False
    warehouse_reports_path: Path = Field(
        default=Path("reports/warehouse"),
        validate_default=True,
    )
    ai_assistant_enabled: bool = False
    ai_access_mode: Literal["pilot_allowlist", "role_based"] = "pilot_allowlist"
    ai_pilot_user_ids: frozenset[int] = frozenset()
    ai_text_enabled: bool = False
    ai_voice_enabled: bool = False
    ai_general_chat_enabled: bool = False
    ai_web_search_enabled: bool = False
    groq_api_key: SecretStr | None = None
    groq_stt_model: str = Field(
        default="whisper-large-v3-turbo",
        min_length=1,
        max_length=100,
    )
    gemini_api_key: SecretStr | None = None
    gemini_fast_model: str = Field(
        default="gemini-3.5-flash-lite",
        min_length=1,
        max_length=100,
    )
    gemini_strong_model: str = Field(
        default="gemini-3.8-flash",
        min_length=1,
        max_length=100,
    )
    ai_voice_max_duration_seconds: int = Field(default=180, gt=0, le=600)
    ai_voice_max_file_bytes: int = Field(
        default=20 * 1024 * 1024,
        gt=0,
        le=50 * 1024 * 1024,
    )
    ai_request_timeout_seconds: int = Field(default=30, gt=0, le=120)
    ai_conversation_ttl_seconds: int = Field(default=86_400, gt=0, le=86_400)
    ai_conversation_max_turns: int = Field(default=12, gt=0, le=20)
    ai_conversation_max_chars: int = Field(default=12_000, gt=0, le=20_000)
    ai_web_search_max_provider_calls_per_update: int = Field(
        default=1,
        ge=1,
        le=1,
    )

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

    @field_validator("ai_pilot_user_ids", mode="before")
    @classmethod
    def parse_ai_pilot_user_ids(cls, value: object) -> frozenset[int]:
        """Parse owner-only pilot IDs without retaining duplicates."""

        return _parse_positive_ids(value, "AI_PILOT_USER_IDS") or frozenset()

    @field_validator("catalog_path", "database_path")
    @classmethod
    def resolve_project_path(cls, path: Path) -> Path:
        """Resolve relative data paths from the repository root."""

        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    @field_validator("vosk_model_path")
    @classmethod
    def resolve_vosk_model_path(cls, path: Path) -> Path:
        """Resolve the model path without allowing relative path traversal."""

        resolved = (
            path.resolve()
            if path.is_absolute()
            else (PROJECT_ROOT / path).resolve()
        )
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError(
                "VOSK_MODEL_PATH должен находиться внутри каталога проекта"
            ) from exc
        return resolved

    @field_validator("warehouse_reports_path")
    @classmethod
    def resolve_warehouse_reports_path(cls, path: Path) -> Path:
        """Keep generated warehouse reports inside the project directory."""

        if path.is_absolute():
            raise ValueError("WAREHOUSE_REPORTS_PATH должен быть относительным")
        resolved = (PROJECT_ROOT / path).resolve()
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError(
                "WAREHOUSE_REPORTS_PATH должен находиться внутри каталога проекта"
            ) from exc
        return resolved

    @property
    def voice_max_file_size_bytes(self) -> int:
        """Return the configured Telegram voice-file limit in bytes."""

        return self.voice_max_file_size_mb * 1024 * 1024

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

    @model_validator(mode="after")
    def validate_ai_provider_keys(self) -> "Settings":
        """Require provider secrets only for explicitly enabled pilot paths."""

        if (
            self.ai_general_chat_enabled or self.ai_web_search_enabled
        ) and not self.ai_assistant_enabled:
            raise ValueError(
                "AI_GENERAL_CHAT_ENABLED/AI_WEB_SEARCH_ENABLED требуют "
                "AI_ASSISTANT_ENABLED"
            )
        if not self.ai_assistant_enabled:
            return self
        if (
            self.ai_general_chat_enabled or self.ai_web_search_enabled
        ) and not (self.ai_text_enabled or self.ai_voice_enabled):
            raise ValueError(
                "AI general/web требуют AI_TEXT_ENABLED или AI_VOICE_ENABLED"
            )
        if self.ai_voice_enabled and (
            self.groq_api_key is None
            or not self.groq_api_key.get_secret_value().strip()
        ):
            raise ValueError(
                "GROQ_API_KEY обязателен при включённом AI voice pilot"
            )
        if (
            self.ai_text_enabled
            or self.ai_voice_enabled
            or self.ai_general_chat_enabled
            or self.ai_web_search_enabled
        ) and (
            self.gemini_api_key is None
            or not self.gemini_api_key.get_secret_value().strip()
        ):
            raise ValueError(
                "GEMINI_API_KEY обязателен при включённом AI text/voice pilot"
            )
        if self.ai_access_mode == "pilot_allowlist" and not self.ai_pilot_user_ids:
            raise ValueError(
                "AI_PILOT_USER_IDS обязателен для закрытого AI pilot"
            )
        return self
