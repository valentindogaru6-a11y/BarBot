"""Centralized Telegram administrator authorization."""

from dataclasses import dataclass


def is_admin(
    user_id: int,
    admin_ids: frozenset[int] | set[int],
) -> bool:
    """Return whether a Telegram user ID has administrator privileges."""

    return user_id in admin_ids


@dataclass(frozen=True, slots=True)
class AuthService:
    """Authorize users exclusively by configured Telegram user IDs."""

    admin_ids: frozenset[int]

    def is_admin(self, user_id: int) -> bool:
        return is_admin(user_id, self.admin_ids)
