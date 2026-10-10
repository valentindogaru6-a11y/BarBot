"""Process-local single-flight coordination for the bounded AI pilot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class AssistantRequestCoordinator:
    """Allow one in-flight AI request per user in the polling process."""

    def __init__(self) -> None:
        self._active_user_ids: set[int] = set()

    @asynccontextmanager
    async def claim(self, user_id: int) -> AsyncIterator[bool]:
        """Yield whether the claim was acquired and always release it."""

        if user_id in self._active_user_ids:
            yield False
            return
        self._active_user_ids.add(user_id)
        try:
            yield True
        finally:
            # No await here: cancellation cannot interrupt release.
            self._active_user_ids.discard(user_id)
