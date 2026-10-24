"""Process-local single-flight coordination for the bounded AI pilot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from collections import OrderedDict
from time import monotonic


class AssistantRequestCoordinator:
    """Allow one in-flight AI request per user in the polling process."""

    def __init__(self) -> None:
        self._active_user_ids: set[int] = set()
        self._completed: OrderedDict[tuple[int, int, int], float] = OrderedDict()

    @asynccontextmanager
    async def claim(self, user_id: int, *, update_key: tuple[int, int] | None = None) -> AsyncIterator[bool]:
        """Yield whether the claim was acquired and always release it."""

        now = monotonic()
        while self._completed and next(iter(self._completed.values())) <= now:
            self._completed.popitem(last=False)
        key = (user_id, *update_key) if update_key is not None and update_key[1] > 0 else None
        if user_id in self._active_user_ids or key in self._completed:
            yield False
            return
        self._active_user_ids.add(user_id)
        try:
            yield True
            if key is not None:
                self._completed[key] = monotonic() + 600
                while len(self._completed) > 1024:
                    self._completed.popitem(last=False)
        finally:
            # No await here: cancellation cannot interrupt release.
            self._active_user_ids.discard(user_id)
