"""Bounded process-local context and conversational history for AI requests."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from time import monotonic
from typing import Callable, Literal


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


class AssistantConversationMemory:
    """Per-user memory that is bounded, expiring and empty after restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30 * 60,
        max_entries: int = 100,
        max_turns: int = 6,
        max_chars: int = 6_000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(ttl_seconds, max_entries, max_turns, max_chars) <= 0:
            raise ValueError("Assistant memory limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._clock = clock
        self._entries: OrderedDict[int, AssistantConversationContext] = (
            OrderedDict()
        )

    def get(self, user_id: int) -> AssistantConversationContext | None:
        self._purge_expired()
        context = self._entries.get(user_id)
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
    ) -> AssistantConversationContext:
        now = self._clock()
        previous = self.get(user_id)
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
        )
        return self._store(user_id, context)

    def append_turn(
        self,
        user_id: int,
        *,
        role: Literal["user", "assistant"],
        text: str,
        last_intent: str | None = None,
        topic: str | None = None,
    ) -> AssistantConversationContext:
        """Append normalized text and evict oldest turns to satisfy all limits."""

        now = self._clock()
        previous = self.get(user_id)
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
        )
        return self._store(user_id, context)

    def provider_history(self, user_id: int) -> tuple[tuple[str, str], ...]:
        context = self.get(user_id)
        if context is None:
            return ()
        return tuple((turn.role, turn.text) for turn in context.turns)

    def refresh(self, user_id: int) -> AssistantConversationContext | None:
        context = self.get(user_id)
        if context is None:
            return None
        refreshed = replace(
            context,
            expires_at=self._clock() + self.ttl_seconds,
        )
        self._entries[user_id] = refreshed
        return refreshed

    def clear(self, user_id: int) -> None:
        self._entries.pop(user_id, None)

    def clear_structured(
        self,
        user_id: int,
        *,
        last_intent: str = "",
        topic: str | None = None,
    ) -> AssistantConversationContext | None:
        """Revoke callbacks/slots while retaining bounded dialog turns."""

        previous = self.get(user_id)
        if previous is None:
            return None
        now = self._clock()
        return self._store(
            user_id,
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
            ),
        )

    def _store(
        self,
        user_id: int,
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
