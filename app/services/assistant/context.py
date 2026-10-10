"""Small process-local structured memory for assistant clarifications."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from time import monotonic
from typing import Callable


@dataclass(frozen=True, slots=True)
class AssistantConversationContext:
    """Only the minimum structured state needed for one follow-up."""

    last_intent: str
    last_entity_type: str
    last_query: str
    candidate_ids: tuple[str, ...]
    pending_slots: tuple[str, ...]
    created_at: float
    expires_at: float


class AssistantConversationMemory:
    """Bounded per-user memory that disappears safely on process restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 15 * 60,
        max_entries: int = 100,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("Assistant memory limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[int, AssistantConversationContext] = (
            OrderedDict()
        )

    def get(self, user_id: int) -> AssistantConversationContext | None:
        self._purge_expired()
        context = self._entries.get(user_id)
        if context is not None:
            self._entries.move_to_end(user_id)
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
    ) -> AssistantConversationContext:
        now = self._clock()
        context = AssistantConversationContext(
            last_intent=last_intent,
            last_entity_type=last_entity_type,
            last_query=" ".join(last_query.split())[:200],
            candidate_ids=tuple(candidate_ids[:5]),
            pending_slots=tuple(pending_slots[:8]),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._entries[user_id] = context
        self._entries.move_to_end(user_id)
        self._purge_expired()
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return context

    def refresh(self, user_id: int) -> AssistantConversationContext | None:
        context = self.get(user_id)
        if context is None:
            return None
        now = self._clock()
        refreshed = replace(context, expires_at=now + self.ttl_seconds)
        self._entries[user_id] = refreshed
        return refreshed

    def clear(self, user_id: int) -> None:
        self._entries.pop(user_id, None)

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
