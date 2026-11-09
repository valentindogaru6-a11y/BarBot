"""Application configuration loaded from environment variables."""

import json
import logging
import re
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
    ai_planner_mode: Literal["local_first", "shadow", "ai_first"] = "local_first"
    ai_planner_primary_provider: Literal["groq", "gemini"] = "gemini"
    ai_planner_fallback_provider: str | None = Field(default="groq", max_length=40)
    ai_planner_fallback_enabled: bool = False
    ai_planner_total_deadline_seconds: int = Field(default=12, ge=1, le=30)
    gemini_planner_model: str | None = Field(default="gemini-3.5-flash-lite", max_length=100)
    groq_planner_fallback_model: str | None = Field(default="openai/gpt-oss-120b", max_length=100)
    gemini_planner_connect_seconds: float = Field(default=3, gt=0, le=3)
    gemini_planner_read_seconds: float = Field(default=4, gt=0, le=4)
    gemini_planner_total_seconds: float = Field(default=5, gt=0, le=5)
    groq_planner_connect_seconds: float = Field(default=3, gt=0, le=3)
    groq_planner_read_seconds: float = Field(default=5, gt=0, le=5)
    groq_planner_total_seconds: float = Field(default=8, gt=0, le=8)
    gemini_planner_min_interval_seconds: float = Field(default=10, ge=10, le=300)
    groq_planner_min_interval_seconds: float = Field(default=15, ge=15, le=300)
    gemini_planner_cooldown_seconds: float = Field(default=90, ge=90, le=900)
    groq_planner_cooldown_seconds: float = Field(default=90, ge=90, le=900)
    ai_planner_physical_concurrency: Literal[1] = 1
    ai_request_global_concurrency: int = Field(default=2, ge=1, le=8)
    ai_request_user_cooldown_seconds: float = Field(default=2, ge=1, le=60)
    ai_request_queue_policy: Literal["immediate_busy"] = "immediate_busy"
    groq_api_key: SecretStr | None = None
    groq_stt_model: str | None = Field(
        default="whisper-large-v3-turbo",
        max_length=100,
    )
    groq_planner_model: str | None = Field(
        default="openai/gpt-oss-20b",
        max_length=100,
    )
    gemini_api_key: SecretStr | None = None
    gemini_fast_model: str | None = Field(
        default="gemini-3.5-flash-lite",
        max_length=100,
    )
    gemini_strong_model: str | None = Field(
        default="gemini-3.8-flash",
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

    @field_validator(
        "groq_stt_model",
        "groq_planner_model",
        "gemini_fast_model",
        "gemini_strong_model",
        "gemini_planner_model",
        "groq_planner_fallback_model",
    )
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        # Only active consumers validate their contract; an unused fallback is
        # not a dependency of startup, including during legacy deployment.
        return value.strip() if value is not None else None

    @property
    def active_planner_providers(self) -> tuple[str, ...]:
        if not self.ai_assistant_enabled or not (self.ai_text_enabled or self.ai_voice_enabled):
            return ()
        return (self.ai_planner_primary_provider, *(
            (self.ai_planner_fallback_provider,) if self.ai_planner_fallback_enabled else ()))

    @property
    def needs_gemini_intent(self) -> bool:
        return bool(self.ai_assistant_enabled and (self.ai_text_enabled or self.ai_voice_enabled) and (
            self.ai_general_chat_enabled or
            (self.ai_planner_mode == 'local_first' and self.ai_planner_primary_provider == 'gemini')))

    def planner_model_for(self, provider: str) -> str | None:
        if provider == 'gemini':
            return self.gemini_planner_model
        if provider == 'groq':
            return (self.groq_planner_model if self.ai_planner_primary_provider == 'groq'
                else self.groq_planner_fallback_model)
        raise ValueError('Unknown AI planner provider')

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
        if self.ai_planner_fallback_enabled:
            if self.ai_planner_fallback_provider not in {'gemini', 'groq'}:
                raise ValueError('Unknown AI planner fallback provider')
            if self.ai_planner_primary_provider == self.ai_planner_fallback_provider:
                raise ValueError("AI planner primary и fallback должны различаться")
        if not self.ai_assistant_enabled:
            return self
        if (
            self.ai_general_chat_enabled or self.ai_web_search_enabled
        ) and not (self.ai_text_enabled or self.ai_voice_enabled):
            raise ValueError(
                "AI general/web требуют AI_TEXT_ENABLED или AI_VOICE_ENABLED"
            )
        def require_key(provider):
            key = getattr(self, provider + '_api_key')
            if key is None or not key.get_secret_value().strip():
                raise ValueError(provider.upper() + '_API_KEY обязателен для активной AI функции')

        def require_model(value):
            if not value or not re.fullmatch(r'[A-Za-z0-9_./-]{1,100}', value):
                raise ValueError('Active AI model name must not be blank or invalid')

        if self.ai_voice_enabled:
            require_key('groq')
            require_model(self.groq_stt_model)
        for provider in self.active_planner_providers:
            require_key(provider)
            require_model(self.planner_model_for(provider))
        if self.needs_gemini_intent:
            require_key('gemini')
            require_model(self.gemini_fast_model)
        if self.ai_web_search_enabled:
            require_key('gemini')
            require_model(self.gemini_strong_model)
        if self.ai_access_mode == "pilot_allowlist" and not self.ai_pilot_user_ids:
            raise ValueError(
                "AI_PILOT_USER_IDS обязателен для закрытого AI pilot"
            )
        return self
