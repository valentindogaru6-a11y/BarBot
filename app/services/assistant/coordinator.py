"""Process-local single-flight coordination for the bounded AI pilot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from collections import OrderedDict
from time import monotonic
import asyncio

from app.services.assistant.planner_policy import planner_request


class AssistantRequestCoordinator:
    """Allow one in-flight AI request per user in the polling process."""

    def __init__(self, *, max_concurrency=2, user_cooldown_seconds=0., clock=monotonic) -> None:
        if not 1 <= max_concurrency <= 8 or not 0 <= user_cooldown_seconds <= 60:
            raise ValueError('invalid AI admission limits')
        self.max_concurrency = max_concurrency
        self.user_cooldown_seconds = user_cooldown_seconds
        self.clock = clock
        self._next_user: OrderedDict[int, float] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self._provider_active = 0
        self._active_user_ids: set[int] = set()
        self._completed: OrderedDict[tuple[int, int, int], float] = OrderedDict()

    @asynccontextmanager
    async def claim(self, user_id: int, *, update_key: tuple[int, int] | None = None, allow_local=False) -> AsyncIterator[bool]:
        """Yield whether the claim was acquired and always release it."""

        now = self.clock()
        while self._completed and next(iter(self._completed.values())) <= now:
            self._completed.popitem(last=False)
        key = (user_id, *update_key) if update_key is not None and update_key[1] > 0 else None
        if self._closing or user_id in self._active_user_ids or key in self._completed:
            yield False
            return
        provider_allowed = (self._provider_active < self.max_concurrency and
            now >= self._next_user.get(user_id, 0))
        if not provider_allowed and not allow_local:
            yield False
            return
        self._active_user_ids.add(user_id)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        if provider_allowed:
            self._provider_active += 1
            self._next_user[user_id] = now + self.user_cooldown_seconds
            self._next_user.move_to_end(user_id)
            while len(self._next_user) > 1024:
                self._next_user.popitem(last=False)
        # Accepted updates remain suppressed on errors/cancellation too. Source
        # identities stay only in bounded process memory, never in diagnostics.
        if key is not None:
            self._completed[key] = now + 600
            while len(self._completed) > 1024:
                self._completed.popitem(last=False)
        try:
            with planner_request(fresh=True, provider_allowed=provider_allowed):
                yield True
        finally:
            # No await here: cancellation cannot interrupt release.
            self._active_user_ids.discard(user_id)
            if provider_allowed:
                self._provider_active -= 1
            if task is not None:
                self._tasks.discard(task)

    async def shutdown(self):
        self._closing = True
        tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
