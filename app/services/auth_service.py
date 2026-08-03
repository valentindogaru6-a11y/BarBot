"""Backward-compatible facade over centralized role permissions."""

from app.services.access_service import AccessService
from app.services.audit_service import AuditService
from app.services.user_service import UserService


def is_admin(
    user_id: int,
    admin_ids: frozenset[int] | set[int],
) -> bool:
    """Return whether a Telegram user ID has administrator privileges."""

    return user_id in admin_ids


class AuthService(AccessService):
    """Keep the existing handler API while resolving SQLite roles."""

    def __init__(
        self,
        admin_ids: frozenset[int],
        user_service: UserService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        super().__init__(admin_ids, user_service, audit)
        self.admin_ids = admin_ids

    def is_admin(self, user_id: int) -> bool:
        return self.can_manage_catalog(user_id)
