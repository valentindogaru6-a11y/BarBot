"""Owner-confirmed long-term memory with no implicit model writes."""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable

from app.storage.assistant_memory_database import AssistantMemoryDatabase


class AssistantMemoryKind(StrEnum):
    TERMINOLOGY = "terminology"
    PRODUCT_ALIAS = "product_alias"
    USER_PREFERENCE = "user_preference"
    WORKFLOW_PREFERENCE = "workflow_preference"
    STANDING_INSTRUCTION = "standing_instruction"
    BUSINESS_FACT = "business_fact"


class AssistantMemoryScope(StrEnum):
    USER = "user"
    BAR = "bar"


class AssistantMemorySource(StrEnum):
    EXPLICIT_REMEMBER = "explicit_remember"
    CONFIRMED_PREVIEW = "confirmed_preview"


@dataclass(frozen=True, slots=True)
class AssistantMemoryRecord:
    token: str
    scope: AssistantMemoryScope
    kind: AssistantMemoryKind
    content: str
    source: AssistantMemorySource
    revision: int
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class AssistantMemoryCommand:
    action: str
    content: str = ""


_REMEMBER_RE = re.compile(r"^\s*запомни(?:\s*,?\s*что)?\s+(.+?)\s*$", re.I)
_FORGET_RE = re.compile(r"^\s*забудь\s+(.+?)\s*$", re.I)
_RECALL_RE = re.compile(r"^\s*что\s+ты(?:\s+(?:обо\s+мне|о\s+баре))?\s+помнишь\s*[?!.]*\s*$", re.I)
_SENSITIVE_RE = re.compile(
    r"(?:api[_ -]?key|bot[_ -]?token|парол|private\s+key|BEGIN\s+.*PRIVATE\s+KEY|"
    r"\b(?:\d[ -]*?){13,19}\b|\b\d{8,10}:[A-Za-z0-9_-]{25,}|"
    r"\b(?:gsk_|sk-|ghp_|github_pat_|AIza)[A-Za-z0-9_-]{20,})",
    re.IGNORECASE,
)


def memory_security_text(content: str) -> str:
    """Normalize spelling/separators for security checks, never for authority."""
    text = unicodedata.normalize("NFKC", content).casefold().replace("ё", "е")
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


_AUTHORITY_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:игнор|ігнор|ignore|disregard|override|bypass|забудь|скасу|отмен|обход|обій|не\s+соблюд|не\s+дотрим)",
    r"(?:system|developer|server|системн|системні|серверн|разработчик|розробник).*(?:правил|rule|instruction|інструкц|инструкц|prompt)",
    r"(?:без|without|no|skip|never|do\s+not|don\s+t|не\s+(?:треб|прос|запит|питай|запраш|вимаг)).*?(?:подтвержд|підтвердж|confirm|permission|провер|перевір)",
    r"(?:роль|роллю|role|права|доступ|permission|allowlist|risk|риск|ризик|super\s*admin|admin|админ|адмін)",
    r"(?:capabilit|tool\s|tools|stable\s*id|executor|исполнител|виконав|инструмент)",
    r"(?:sql|shell|bash|powershell|systemctl|systemd|deploy|filesystem|файлов|файлів|командн|терминал|термінал)",
    r"(?:drop|alter|truncate|delete|insert|update|select|create)\s+(?:table|from|into|database|index)",
    r"(?:выполн|викона|execute|run|запуст|запусти|виконати).*(?:команд|command|скрипт|script)",
    r"(?:выполн|викона|execute|run|запуст).*(?:python|subprocess|os\s+system|код|code)",
    r"(?:удал|видал|прочит|читай|открой|відкрий|запиш|створ|созда|open|read|write|delete|remove|create).*(?:файл|file|директор|folder|directory)",
    r"(?:раскр|покаж|вывед|розкр|reveal|show|print|leak|expose).*(?:внутрен|прихован|внутріш|hidden|internal).*(?:prompt|instruction|інструкц|инструкц|промпт)",
    r"(?:следуй|дотримуй|follow|obey).*(?:моим|моїм|только|лишь|only|my|user).*(?:инструк|правил|інструк|instruction|rule)",
    r"(?:автомат|auto).*(?:подтверж|підтвер|confirm)",
    r"(?:ключ|токен|token|secret|парол|password|api\s*key|private\s*key|внутренн.*промпт|internal.*prompt|system\s*prompt|\.env|\benv\b)",
))


def validate_memory_content(content: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", content).split()).strip()
    normalized = memory_security_text(value)
    if not value or len(value) > 2000:
        raise ValueError("Memory content is empty or too long")
    compact = normalized.replace(' ', '')
    if (_SENSITIVE_RE.search(value) or any(p.search(normalized) for p in _AUTHORITY_PATTERNS)
            or any(marker in compact for marker in ('systemctl','superadmin','shellexecute','ignoreinstructions','игнорируйправила'))):
        raise ValueError("Sensitive or authority-changing memory is not allowed")
    return value


def parse_memory_command(text: str) -> AssistantMemoryCommand | None:
    if _RECALL_RE.match(text):
        return AssistantMemoryCommand("recall")
    match = _REMEMBER_RE.match(text)
    if match:
        return AssistantMemoryCommand("preview_save", " ".join(match.group(1).split())[:2000])
    match = _FORGET_RE.match(text)
    if match:
        return AssistantMemoryCommand("select_delete", " ".join(match.group(1).split())[:500])
    return None


class AssistantLongTermMemoryService:
    def __init__(self, database: AssistantMemoryDatabase, *, clock: Callable[[], datetime] | None = None) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Assistant memory clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def normalize(content: str) -> str:
        return validate_memory_content(content)

    def list(self, owner_user_id: int, *, source_chat_id: int | None = None, limit: int = 12) -> tuple[AssistantMemoryRecord, ...]:
        return tuple(
            AssistantMemoryRecord(
                token=str(row["opaque_token"]), scope=AssistantMemoryScope(row["scope"]),
                kind=AssistantMemoryKind(row["kind"]), content=str(row["normalized_content"]),
                source=AssistantMemorySource(row["source"]), revision=int(row["revision"]),
                expires_at=str(row["expires_at_utc"]) if row["expires_at_utc"] else None,
            )
            for row in self.database.list_active(owner_user_id, self._now(), source_chat_id=source_chat_id, limit=limit)
        )

    def save_confirmed(self, owner_user_id: int, *, scope: AssistantMemoryScope,
                       kind: AssistantMemoryKind, content: str,
                       source: AssistantMemorySource,
                       supersedes_token: str | None = None,
                       source_chat_id: int | None = None) -> AssistantMemoryRecord:
        normalized = self.normalize(content)
        row = self.database.create(
            token=secrets.token_urlsafe(16), owner_user_id=owner_user_id,
            scope=scope.value, kind=kind.value, content=normalized,
            source=source.value, now=self._now(), supersedes_token=supersedes_token,
            source_chat_id=source_chat_id,
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=scope, kind=kind,
            content=normalized, source=source, revision=int(row["revision"]),
            expires_at=None,
        )

    def create_preview(self, owner_user_id: int, *, scope: AssistantMemoryScope,
                       kind: AssistantMemoryKind, content: str,
                       source: AssistantMemorySource,
                       source_chat_id: int | None = None) -> AssistantMemoryRecord:
        if source is not AssistantMemorySource.EXPLICIT_REMEMBER:
            raise ValueError("Memory previews require an explicit user request")
        normalized = self.normalize(content)
        now_dt = datetime.fromisoformat(self._now().replace('Z', '+00:00'))
        row = self.database.create(
            token=secrets.token_urlsafe(16), owner_user_id=owner_user_id,
            scope=scope.value, kind=kind.value, content=normalized,
            source=source.value, status="pending",
            now=now_dt.isoformat().replace("+00:00", "Z"),
            expires_at=(now_dt + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
            source_chat_id=source_chat_id,
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=scope, kind=kind,
            content=normalized, source=source, revision=int(row["revision"]),
            expires_at=str(row["expires_at_utc"]),
        )

    def confirm_preview(self, owner_user_id: int, *, token: str, revision: int,
                        source_chat_id: int | None = None,
                        authorize: Callable[[], None] | None = None) -> AssistantMemoryRecord:
        existing = self.database.get_owned(owner_user_id, token, source_chat_id=source_chat_id)
        if existing is None:
            raise ValueError("Memory preview missing")
        self.normalize(str(existing['normalized_content']))
        AssistantMemoryKind(existing['kind'])
        row = self.database.transition(
            owner_user_id, token, expected_revision=revision,
            expected_status="pending", status="active", now=self._now(),
            source_chat_id=source_chat_id,
            authorize=authorize,
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=AssistantMemoryScope(row["scope"]),
            kind=AssistantMemoryKind(row["kind"]), content=str(row["normalized_content"]),
            source=AssistantMemorySource(row["source"]), revision=int(row["revision"]),
            expires_at=None,
        )

    def cancel_preview(self, owner_user_id: int, *, token: str, revision: int,
                       source_chat_id: int | None = None,
                       authorize: Callable[[], None] | None = None) -> None:
        self.database.transition(
            owner_user_id, token, expected_revision=revision,
            expected_status="pending", status="cancelled", now=self._now(),
            source_chat_id=source_chat_id,
            authorize=authorize,
        )

    def delete_confirmed(self, owner_user_id: int, *, token: str, revision: int,
                         source_chat_id: int | None = None) -> None:
        self.database.delete(owner_user_id, token, expected_revision=revision, now=self._now(), source_chat_id=source_chat_id)

    def prepare_delete(self, owner: int, chat: int, *, token: str, revision: int) -> AssistantMemoryRecord:
        now = self._now()
        expires = (datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
        preview_token = secrets.token_urlsafe(16)
        row = self.database.prepare_delete(owner, chat, token, revision, preview_token=preview_token, now=now, expires=expires)
        return AssistantMemoryRecord(
            token=preview_token, scope=AssistantMemoryScope(row['scope']),
            kind=AssistantMemoryKind(row['kind']), content=str(row['normalized_content']),
            source=AssistantMemorySource.CONFIRMED_PREVIEW, revision=revision, expires_at=expires,
        )

    def finish_delete(self, owner: int, chat: int, *, token: str, revision: int, cancel: bool = False,
                      authorize: Callable[[], None] | None = None) -> None:
        self.database.finish_delete(owner, chat, token, revision, now=self._now(), cancel=cancel, authorize=authorize)
