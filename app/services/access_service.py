"""Centralized role resolution, permissions, and protected role changes."""

from dataclasses import dataclass

from app.models.user import Role, UserRecord
from app.services.audit_service import AuditService
from app.services.user_service import UserService


class AccessDeniedError(PermissionError):
    """Raised when an actor cannot perform a requested role change."""


@dataclass(frozen=True, slots=True)
class UserContext:
    telegram_user_id: int
    role: Role
    user: UserRecord | None

    @property
    def can_manage_catalog(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_manage_users(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_assign_admin(self) -> bool:
        return self.role is Role.SUPER_ADMIN

    @property
    def can_assign_bartender(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_view_catalog(self) -> bool:
        return self.role is not Role.BLOCKED


class AccessService:
    """Use environment owners plus SQLite as the sole permission source."""

    def __init__(
        self,
        super_admin_ids: frozenset[int],
        user_service: UserService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.super_admin_ids = super_admin_ids
        self.user_service = user_service
        self.audit = audit

    def get_context(self, telegram_user_id: int) -> UserContext:
        user = (
            self.user_service.get_user(telegram_user_id)
            if self.user_service is not None
            else None
        )
        role = (
            Role.SUPER_ADMIN
            if telegram_user_id in self.super_admin_ids
            else user.role if user is not None else Role.USER
        )
        return UserContext(telegram_user_id, role, user)

    def can_view_catalog(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_view_catalog

    def can_manage_catalog(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_manage_catalog

    def can_manage_users(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_manage_users

    def can_assign_admin(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_assign_admin

    def can_assign_bartender(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_assign_bartender

    def can_change_role(
        self,
        actor_user_id: int,
        target_user_id: int,
        new_role: Role,
    ) -> bool:
        actor = self.get_context(actor_user_id)
        target = self.get_context(target_user_id)
        if not actor.can_manage_users:
            return False
        if target.role is Role.SUPER_ADMIN or actor_user_id == target_user_id:
            return False
        if actor.role is Role.SUPER_ADMIN:
            return new_role in {
                Role.ADMIN,
                Role.BARTENDER,
                Role.USER,
                Role.BLOCKED,
            }
        if target.role is Role.ADMIN or new_role is Role.ADMIN:
            return False
        return new_role in {Role.BARTENDER, Role.USER, Role.BLOCKED}

    def available_role_changes(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> list[Role]:
        target = self.get_context(target_user_id)
        return [
            role
            for role in (Role.ADMIN, Role.BARTENDER, Role.USER, Role.BLOCKED)
            if role is not target.role
            and self.can_change_role(actor_user_id, target_user_id, role)
        ]

    def change_role(
        self,
        actor_user_id: int,
        target_user_id: int,
        new_role: Role,
    ) -> UserRecord:
        """Recompute permissions immediately before the SQLite write."""

        target = self.get_context(target_user_id)
        if not self.can_change_role(actor_user_id, target_user_id, new_role):
            self._audit_role_change(
                actor_user_id,
                target_user_id,
                target.role,
                new_role,
                "role_change_denied",
                "error",
                "Изменение роли отклонено проверкой разрешений",
            )
            raise AccessDeniedError("Недостаточно прав")
        if self.user_service is None:
            raise RuntimeError("UserService is required for role changes")
        previous_role, updated = self.user_service.set_role(
            target_user_id,
            new_role,
            actor_user_id=actor_user_id,
        )
        action = (
            "user_blocked"
            if new_role is Role.BLOCKED
            else "user_unblocked"
            if previous_role is Role.BLOCKED
            else "role_changed"
        )
        self._audit_role_change(
            actor_user_id,
            target_user_id,
            previous_role,
            new_role,
            action,
            "success",
            "Роль пользователя изменена",
        )
        return updated

    def _audit_role_change(
        self,
        actor_user_id: int,
        target_user_id: int,
        previous_role: Role,
        new_role: Role,
        action: str,
        status: str,
        description: str,
    ) -> None:
        if self.audit is not None:
            self.audit.record_user_event(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                previous_role=previous_role.value,
                new_role=new_role.value,
                action=action,
                status=status,
                description=description,
            )
