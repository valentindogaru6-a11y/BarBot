"""Bounded conversation context with process-local and SQLite backends."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import json
from time import monotonic
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal

from app.models.assistant import AssistantRelation, AssistantRequestedKind
from app.storage.assistant_conversation_database import (
    AssistantConversationConflict,
    AssistantConversationDatabase,
)


@dataclass(frozen=True, slots=True)
class AssistantConversationTurn:
    """One dialog turn passed to a provider without a Telegram identifier."""

    role: Literal["user", "assistant"]
    text: str
    timestamp: float


@dataclass(frozen=True, slots=True)
class AssistantConversationContext:
    """Structured clarification state plus a small natural-language history."""

    last_intent: str
    last_entity_type: str
    last_query: str
    candidate_ids: tuple[str, ...]
    pending_slots: tuple[str, ...]
    topic: str
    turns: tuple[AssistantConversationTurn, ...]
    created_at: float
    expires_at: float
    authorized_action: str = ""
    summary_text: str = ""
    last_requested_action: str = ""
    last_entity_id: str = ""
    last_entity_label: str = ""
    last_tool_name: str = ""
    requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED
    relation: AssistantRelation = AssistantRelation.UNSPECIFIED
    relation_target: str = ""
    parent_entity_id: str = ""
    related_entity_ids: tuple[str, ...] = ()
    selected_entity_id: str = ""
    revision: int = 1


class AssistantConversationMemory:
    """Per-user memory that is bounded, expiring and empty after restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 24 * 60 * 60,
        max_entries: int = 100,
        max_turns: int = 12,
        max_chars: int = 12_000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(ttl_seconds, max_entries, max_turns, max_chars) <= 0:
            raise ValueError("Assistant memory limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._clock = clock
        self._entries: OrderedDict[tuple[int, int], AssistantConversationContext] = (
            OrderedDict()
        )

    @staticmethod
    def _key(user_id: int, chat_id: int | None) -> tuple[int, int]:
        return user_id, user_id if chat_id is None else chat_id

    def get(self, user_id: int, chat_id: int | None = None) -> AssistantConversationContext | None:
        self._purge_expired()
        context = self._entries.get(self._key(user_id, chat_id))
        return context

    def remember(
        self,
        user_id: int,
        *,
        last_intent: str,
        last_entity_type: str,
        last_query: str,
        candidate_ids: tuple[str, ...] = (),
        pending_slots: tuple[str, ...] = (),
        topic: str | None = None,
        authorized_action: str = "",
        chat_id: int | None = None,
        summary_text: str | None = None,
        last_requested_action: str = "",
        last_entity_id: str = "",
        last_entity_label: str = "",
        last_tool_name: str = "",
        requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED,
        relation: AssistantRelation = AssistantRelation.UNSPECIFIED,
        relation_target: str = "",
        parent_entity_id: str = "",
        related_entity_ids: tuple[str, ...] = (),
        selected_entity_id: str = "",
    ) -> AssistantConversationContext:
        now = self._clock()
        previous = self.get(user_id, chat_id)
        context = AssistantConversationContext(
            last_intent=last_intent[:100],
            last_entity_type=last_entity_type[:100],
            last_query=" ".join(last_query.split())[:200],
            candidate_ids=tuple(candidate_ids[:5]),
            pending_slots=tuple(pending_slots[:8]),
            topic=" ".join((topic or (previous.topic if previous else "")).split())[:200],
            turns=previous.turns if previous else (),
            created_at=previous.created_at if previous else now,
            expires_at=now + self.ttl_seconds,
            authorized_action=authorized_action[:100],
            summary_text=(summary_text if summary_text is not None else (previous.summary_text if previous else ""))[:2000],
            last_requested_action=last_requested_action[:100],
            last_entity_id=last_entity_id[:300],
            last_entity_label=last_entity_label[:500],
            last_tool_name=last_tool_name[:100],
            requested_kind=requested_kind,
            relation=relation,
            relation_target=relation_target[:300],
            parent_entity_id=parent_entity_id[:300],
            related_entity_ids=tuple(related_entity_ids[:5]),
            selected_entity_id=selected_entity_id[:300],
            revision=(previous.revision + 1) if previous else 1,
        )
        return self._store(self._key(user_id, chat_id), context)

    def append_turn(
        self,
        user_id: int,
        *,
        role: Literal["user", "assistant"],
        text: str,
        last_intent: str | None = None,
        topic: str | None = None,
        chat_id: int | None = None,
    ) -> AssistantConversationContext:
        """Append normalized text and evict oldest turns to satisfy all limits."""

        now = self._clock()
        previous = self.get(user_id, chat_id)
        cleaned = " ".join(text.split())[: self.max_chars]
        turns = list(previous.turns if previous else ())
        if cleaned:
            turns.append(AssistantConversationTurn(role, cleaned, now))
        while len(turns) > self.max_turns:
            turns.pop(0)
        while turns and sum(len(turn.text) for turn in turns) > self.max_chars:
            turns.pop(0)
        context = AssistantConversationContext(
            last_intent=(last_intent or (previous.last_intent if previous else ""))[:100],
            last_entity_type=previous.last_entity_type if previous else "conversation",
            last_query=previous.last_query if previous else "",
            candidate_ids=previous.candidate_ids if previous else (),
            pending_slots=previous.pending_slots if previous else (),
            topic=" ".join((topic or (previous.topic if previous else "")).split())[:200],
            turns=tuple(turns),
            created_at=previous.created_at if previous else now,
            expires_at=now + self.ttl_seconds,
            authorized_action=previous.authorized_action if previous else "",
            summary_text=previous.summary_text if previous else "",
            last_requested_action=previous.last_requested_action if previous else "",
            last_entity_id=previous.last_entity_id if previous else "",
            last_entity_label=previous.last_entity_label if previous else "",
            last_tool_name=previous.last_tool_name if previous else "",
            requested_kind=previous.requested_kind if previous else AssistantRequestedKind.UNSPECIFIED,
            relation=previous.relation if previous else AssistantRelation.UNSPECIFIED,
            relation_target=previous.relation_target if previous else "",
            parent_entity_id=previous.parent_entity_id if previous else "",
            related_entity_ids=previous.related_entity_ids if previous else (),
            selected_entity_id=previous.selected_entity_id if previous else "",
            revision=(previous.revision + 1) if previous else 1,
        )
        return self._store(self._key(user_id, chat_id), context)

    def provider_history(self, user_id: int, chat_id: int | None = None) -> tuple[tuple[str, str], ...]:
        context = self.get(user_id, chat_id)
        if context is None:
            return ()
        return tuple((turn.role, turn.text) for turn in context.turns)

    def refresh(self, user_id: int, chat_id: int | None = None) -> AssistantConversationContext | None:
        context = self.get(user_id, chat_id)
        if context is None:
            return None
        refreshed = replace(
            context,
            expires_at=self._clock() + self.ttl_seconds,
        )
        self._entries[self._key(user_id, chat_id)] = refreshed
        return refreshed

    def clear(self, user_id: int, chat_id: int | None = None) -> None:
        self._entries.pop(self._key(user_id, chat_id), None)

    def clear_structured(
        self,
        user_id: int,
        *,
        last_intent: str = "",
        topic: str | None = None,
        chat_id: int | None = None,
    ) -> AssistantConversationContext | None:
        """Revoke callbacks/slots while retaining bounded dialog turns."""

        previous = self.get(user_id, chat_id)
        if previous is None:
            return None
        now = self._clock()
        return self._store(
            self._key(user_id, chat_id),
            AssistantConversationContext(
                last_intent=last_intent[:100],
                last_entity_type="conversation",
                last_query="",
                candidate_ids=(),
                pending_slots=(),
                topic=" ".join((topic or previous.topic).split())[:200],
                turns=previous.turns,
                created_at=previous.created_at,
                expires_at=now + self.ttl_seconds,
                authorized_action="",
                summary_text=previous.summary_text,
                last_requested_action="",
                last_entity_id="",
                last_entity_label="",
                last_tool_name="",
                requested_kind=AssistantRequestedKind.UNSPECIFIED,
                relation=AssistantRelation.UNSPECIFIED,
                relation_target="",
                parent_entity_id="",
                related_entity_ids=(),
                selected_entity_id="",
                revision=previous.revision + 1,
            ),
        )

    def _store(
        self,
        user_id: tuple[int, int],
        context: AssistantConversationContext,
    ) -> AssistantConversationContext:
        self._entries[user_id] = context
        self._purge_expired()
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return context

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            user_id
            for user_id, context in self._entries.items()
            if context.expires_at <= now
        ]
        for user_id in expired:
            self._entries.pop(user_id, None)

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._entries)


class PersistentAssistantConversationMemory:
    """SQLite-backed bounded context isolated by user and private chat."""

    def __init__(
        self,
        database: AssistantConversationDatabase,
        *,
        ttl_seconds: int = 86_400,
        max_turns: int = 12,
        max_chars: int = 12_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if min(ttl_seconds, max_turns, max_chars) <= 0:
            raise ValueError("Assistant memory limits must be positive")
        self.database = database
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Assistant memory clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> float:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    @staticmethod
    def _decode_candidate_context(raw: object) -> dict[str, object]:
        decoded = json.loads(str(raw))
        if isinstance(decoded, list):
            return {"candidate_ids": decoded}
        if not isinstance(decoded, dict):
            raise ValueError("Invalid assistant candidate context")
        return decoded

    def get(self, user_id: int, chat_id: int | None = None) -> AssistantConversationContext | None:
        actual_chat_id = user_id if chat_id is None else chat_id
        loaded = self.database.load(user_id, actual_chat_id, self._stamp(self._now()))
        if loaded is None:
            return None
        row, raw_turns = loaded
        candidate_context = self._decode_candidate_context(
            row["last_candidate_ids_json"]
        )
        candidates = tuple(candidate_context.get("candidate_ids", ()))
        turns = tuple(
            AssistantConversationTurn(
                role=turn["role"],
                text=str(turn["content"]),
                timestamp=self._parse(str(turn["created_at_utc"])),
            )
            for turn in raw_turns
        )
        return AssistantConversationContext(
            last_intent=str(row["current_intent"]),
            last_entity_type=str(row["last_entity_type"]),
            last_query=str(row["last_catalog_query"]),
            candidate_ids=candidates[:5],
            pending_slots=(str(row["pending_clarification"]),) if row["pending_clarification"] else (),
            topic=str(row["current_topic"]),
            turns=turns,
            created_at=self._parse(str(row["started_at_utc"])),
            expires_at=self._parse(str(row["expires_at_utc"])),
            authorized_action=str(row["authorized_action"]),
            summary_text=str(row["summary_text"]),
            last_requested_action=str(row["last_requested_action"]),
            last_entity_id=str(row["last_entity_id"]),
            last_entity_label=str(row["last_entity_label"]),
            last_tool_name=str(row["last_tool_name"]),
            requested_kind=AssistantRequestedKind(
                candidate_context.get("requested_kind", "unspecified")
            ),
            relation=AssistantRelation(
                candidate_context.get("relation", "unspecified")
            ),
            relation_target=str(candidate_context.get("relation_target", "")),
            parent_entity_id=str(candidate_context.get("parent_entity_id", "")),
            related_entity_ids=tuple(
                candidate_context.get("related_entity_ids", candidates)
            )[:5],
            selected_entity_id=str(
                candidate_context.get("selected_entity_id", row["last_entity_id"])
            ),
            revision=int(row["revision"]),
        )

    def _save(self, user_id: int, chat_id: int, context: AssistantConversationContext, expected: int | None) -> AssistantConversationContext:
        now = self._now()
        start = datetime.fromtimestamp(context.created_at, UTC)
        data = {
            "started_at_utc": self._stamp(start),
            "last_activity_at_utc": self._stamp(now),
            "expires_at_utc": self._stamp(now + timedelta(seconds=self.ttl_seconds)),
            "summary_text": context.summary_text[:2000],
            "current_topic": context.topic[:500],
            "current_intent": context.last_intent[:100],
            "last_requested_action": context.last_requested_action[:100],
            "last_catalog_query": context.last_query[:500],
            "last_entity_type": context.last_entity_type[:100],
            "last_entity_id": context.last_entity_id[:300],
            "last_entity_label": context.last_entity_label[:500],
            "candidate_ids": context.candidate_ids[:5],
            "candidate_context": {
                "candidate_ids": context.candidate_ids[:5],
                "requested_kind": context.requested_kind.value,
                "relation": context.relation.value,
                "relation_target": context.relation_target[:300],
                "parent_entity_id": context.parent_entity_id[:300],
                **(
                    {"related_entity_ids": context.related_entity_ids[:5]}
                    if context.related_entity_ids != context.candidate_ids
                    else {}
                ),
                **(
                    {"selected_entity_id": context.selected_entity_id[:300]}
                    if context.selected_entity_id != context.last_entity_id
                    else {}
                ),
            },
            "last_tool_name": context.last_tool_name[:100],
            "pending_clarification": context.pending_slots[0][:500] if context.pending_slots else "",
            "authorized_action": context.authorized_action[:100],
        }
        turns = [
            {"role": turn.role, "content": turn.text, "created_at_utc": self._stamp(datetime.fromtimestamp(turn.timestamp, UTC))}
            for turn in context.turns
        ]
        revision = self.database.save(user_id, chat_id, data, turns, expected_revision=expected, now=self._stamp(now))
        return replace(context, revision=revision, expires_at=(now + timedelta(seconds=self.ttl_seconds)).timestamp())

    def remember(self, user_id: int, *, last_intent: str, last_entity_type: str, last_query: str, candidate_ids: tuple[str, ...] = (), pending_slots: tuple[str, ...] = (), topic: str | None = None, authorized_action: str = "", chat_id: int | None = None, summary_text: str | None = None, last_requested_action: str = "", last_entity_id: str = "", last_entity_label: str = "", last_tool_name: str = "", requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED, relation: AssistantRelation = AssistantRelation.UNSPECIFIED, relation_target: str = "", parent_entity_id: str = "", related_entity_ids: tuple[str, ...] = (), selected_entity_id: str = "") -> AssistantConversationContext:
        actual_chat_id = user_id if chat_id is None else chat_id
        previous = self.get(user_id, actual_chat_id)
        now = self._now().timestamp()
        context = AssistantConversationContext(
            last_intent=last_intent[:100], last_entity_type=last_entity_type[:100],
            last_query=" ".join(last_query.split())[:500], candidate_ids=tuple(candidate_ids[:5]),
            pending_slots=tuple(pending_slots[:1]), topic=" ".join((topic or (previous.topic if previous else "")).split())[:500],
            turns=previous.turns if previous else (), created_at=previous.created_at if previous else now,
            expires_at=now + self.ttl_seconds, authorized_action=authorized_action[:100],
            summary_text=(summary_text if summary_text is not None else (previous.summary_text if previous else ""))[:2000],
            last_requested_action=last_requested_action[:100], last_entity_id=last_entity_id[:300],
            last_entity_label=last_entity_label[:500], last_tool_name=last_tool_name[:100],
            requested_kind=requested_kind, relation=relation,
            relation_target=relation_target[:300], parent_entity_id=parent_entity_id[:300],
            related_entity_ids=tuple(related_entity_ids[:5]), selected_entity_id=selected_entity_id[:300],
            revision=previous.revision if previous else 0,
        )
        return self._save(user_id, actual_chat_id, context, previous.revision if previous else None)

    def append_turn(self, user_id: int, *, role: Literal["user", "assistant"], text: str, last_intent: str | None = None, topic: str | None = None, chat_id: int | None = None) -> AssistantConversationContext:
        actual_chat_id = user_id if chat_id is None else chat_id
        for attempt in range(2):
            previous = self.get(user_id, actual_chat_id)
            now = self._now().timestamp()
            cleaned = " ".join(text.split())[:self.max_chars]
            turns = list(previous.turns if previous else ())
            if cleaned:
                turns.append(AssistantConversationTurn(role, cleaned, now))
            while len(turns) > self.max_turns or sum(len(turn.text) for turn in turns) > self.max_chars:
                turns.pop(0)
            context = replace(
                previous or AssistantConversationContext("", "conversation", "", (), (), "", (), now, now + self.ttl_seconds),
                last_intent=(last_intent or (previous.last_intent if previous else ""))[:100],
                topic=" ".join((topic or (previous.topic if previous else "")).split())[:500],
                turns=tuple(turns),
            )
            try:
                return self._save(user_id, actual_chat_id, context, previous.revision if previous else None)
            except AssistantConversationConflict:
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def provider_history(self, user_id: int, chat_id: int | None = None) -> tuple[tuple[str, str], ...]:
        context = self.get(user_id, chat_id)
        return tuple((turn.role, turn.text) for turn in context.turns) if context else ()

    def refresh(self, user_id: int, chat_id: int | None = None) -> AssistantConversationContext | None:
        context = self.get(user_id, chat_id)
        if context is None:
            return None
        actual_chat_id = user_id if chat_id is None else chat_id
        return self._save(user_id, actual_chat_id, context, context.revision)

    def clear(self, user_id: int, chat_id: int | None = None) -> None:
        self.database.clear(user_id, user_id if chat_id is None else chat_id)

    def clear_structured(self, user_id: int, *, last_intent: str = "", topic: str | None = None, chat_id: int | None = None) -> AssistantConversationContext | None:
        previous = self.get(user_id, chat_id)
        if previous is None:
            return None
        return self.remember(
            user_id, chat_id=chat_id, last_intent=last_intent,
            last_entity_type="conversation", last_query="", topic=topic or previous.topic,
            summary_text=previous.summary_text,
        )

    def __len__(self) -> int:
        raise TypeError("Persistent memory does not expose a global session count")
