"""Owner-confirmed long-term memory with no implicit model writes."""

from __future__ import annotations

import re
import secrets
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
    r"\b(?:\d[ -]*?){13,19}\b)",
    re.IGNORECASE,
)


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
        value = " ".join(content.split()).strip()
        if not value or len(value) > 2000:
            raise ValueError("Memory content is empty or too long")
        if _SENSITIVE_RE.search(value):
            raise ValueError("Sensitive content cannot be stored in assistant memory")
        return value

    def list(self, owner_user_id: int, *, limit: int = 12) -> tuple[AssistantMemoryRecord, ...]:
        return tuple(
            AssistantMemoryRecord(
                token=str(row["opaque_token"]), scope=AssistantMemoryScope(row["scope"]),
                kind=AssistantMemoryKind(row["kind"]), content=str(row["normalized_content"]),
                source=AssistantMemorySource(row["source"]), revision=int(row["revision"]),
                expires_at=str(row["expires_at_utc"]) if row["expires_at_utc"] else None,
            )
            for row in self.database.list_active(owner_user_id, self._now(), limit=limit)
        )

    def save_confirmed(self, owner_user_id: int, *, scope: AssistantMemoryScope,
                       kind: AssistantMemoryKind, content: str,
                       source: AssistantMemorySource,
                       supersedes_token: str | None = None) -> AssistantMemoryRecord:
        normalized = self.normalize(content)
        row = self.database.create(
            token=secrets.token_urlsafe(16), owner_user_id=owner_user_id,
            scope=scope.value, kind=kind.value, content=normalized,
            source=source.value, now=self._now(), supersedes_token=supersedes_token,
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=scope, kind=kind,
            content=normalized, source=source, revision=int(row["revision"]),
            expires_at=None,
        )

    def create_preview(self, owner_user_id: int, *, scope: AssistantMemoryScope,
                       kind: AssistantMemoryKind, content: str,
                       source: AssistantMemorySource) -> AssistantMemoryRecord:
        normalized = self.normalize(content)
        now_dt = self._clock().astimezone(UTC)
        row = self.database.create(
            token=secrets.token_urlsafe(16), owner_user_id=owner_user_id,
            scope=scope.value, kind=kind.value, content=normalized,
            source=source.value, status="pending",
            now=now_dt.isoformat().replace("+00:00", "Z"),
            expires_at=(now_dt + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=scope, kind=kind,
            content=normalized, source=source, revision=int(row["revision"]),
            expires_at=str(row["expires_at_utc"]),
        )

    def confirm_preview(self, owner_user_id: int, *, token: str, revision: int) -> AssistantMemoryRecord:
        row = self.database.transition(
            owner_user_id, token, expected_revision=revision,
            expected_status="pending", status="active", now=self._now(),
        )
        return AssistantMemoryRecord(
            token=str(row["opaque_token"]), scope=AssistantMemoryScope(row["scope"]),
            kind=AssistantMemoryKind(row["kind"]), content=str(row["normalized_content"]),
            source=AssistantMemorySource(row["source"]), revision=int(row["revision"]),
            expires_at=None,
        )

    def cancel_preview(self, owner_user_id: int, *, token: str, revision: int) -> None:
        self.database.transition(
            owner_user_id, token, expected_revision=revision,
            expected_status="pending", status="cancelled", now=self._now(),
        )

    def delete_confirmed(self, owner_user_id: int, *, token: str, revision: int) -> None:
        self.database.delete(owner_user_id, token, expected_revision=revision, now=self._now())
