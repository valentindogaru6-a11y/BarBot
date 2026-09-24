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
    def can_manage_warehouse_nomenclature(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_view_warehouse(self) -> bool:
        return self.role in {
            Role.SUPER_ADMIN,
            Role.ADMIN,
            Role.BARTENDER,
            Role.ACCOUNTANT,
        }

    @property
    def can_use_warehouse_attendance(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER}

    @property
    def can_manage_warehouse_attendance(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_mutate_warehouse(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER}

    @property
    def can_manage_warehouse_required_rules(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_manage_warehouse_report_recipients(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_manage_warehouse_reminders(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_manage_warehouse_delivery_schedules(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_manage_warehouse_corrections(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_view_warehouse_corrections(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN, Role.ACCOUNTANT}

    @property
    def can_send_warehouse_reports(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER}

    @property
    def can_view_warehouse_delivery_history(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN, Role.ACCOUNTANT}

    @property
    def can_assign_admin(self) -> bool:
        return self.role is Role.SUPER_ADMIN

    @property
    def can_assign_bartender(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_assign_accountant(self) -> bool:
        return self.role in {Role.SUPER_ADMIN, Role.ADMIN}

    @property
    def can_view_catalog(self) -> bool:
        return self.role in {
            Role.SUPER_ADMIN,
            Role.ADMIN,
            Role.BARTENDER,
            Role.USER,
        }


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

    def can_manage_warehouse_nomenclature(self, telegram_user_id: int) -> bool:
        return self.get_context(
            telegram_user_id
        ).can_manage_warehouse_nomenclature

    def can_view_warehouse(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_view_warehouse

    def can_mutate_warehouse(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_mutate_warehouse

    def can_use_warehouse_attendance(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_use_warehouse_attendance

    def can_manage_warehouse_required_rules(self, telegram_user_id: int) -> bool:
        return self.get_context(
            telegram_user_id
        ).can_manage_warehouse_required_rules

    def can_manage_warehouse_report_recipients(
        self, telegram_user_id: int
    ) -> bool:
        return self.get_context(
            telegram_user_id
        ).can_manage_warehouse_report_recipients

    def can_manage_warehouse_reminders(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_manage_warehouse_reminders

    def require_warehouse_reminder_management(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_reminders:
            raise AccessDeniedError("Недостаточно прав для управления напоминаниями")
        return context

    def require_warehouse_delivery_schedule_management(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_delivery_schedules:
            raise AccessDeniedError("Недостаточно прав для управления расписаниями")
        return context

    def require_warehouse_correction_management(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_corrections:
            raise AccessDeniedError("Недостаточно прав для корректировки закрытой смены")
        return context

    def require_warehouse_correction_view(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_view_warehouse_corrections:
            raise AccessDeniedError("Недостаточно прав для просмотра исправлений")
        return context

    def can_send_warehouse_reports(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_send_warehouse_reports

    def can_view_warehouse_delivery_history(self, telegram_user_id: int) -> bool:
        return self.get_context(
            telegram_user_id
        ).can_view_warehouse_delivery_history

    def require_warehouse_view(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_view_warehouse:
            raise AccessDeniedError("Недостаточно прав для просмотра склада")
        return context

    def require_warehouse_attendance(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_use_warehouse_attendance:
            raise AccessDeniedError("Недостаточно прав для рабочей смены")
        return context

    def require_warehouse_attendance_management(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_attendance:
            raise AccessDeniedError("Недостаточно прав для управления рабочими сменами")
        return context

    def require_warehouse_mutation(self, telegram_user_id: int) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_mutate_warehouse:
            raise AccessDeniedError("Недостаточно прав для изменения склада")
        return context

    def require_warehouse_required_rule_management(
        self,
        telegram_user_id: int,
    ) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_required_rules:
            raise AccessDeniedError(
                "Недостаточно прав для обязательных списаний"
            )
        return context

    def require_warehouse_report_recipient_management(
        self, telegram_user_id: int
    ) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_manage_warehouse_report_recipients:
            raise AccessDeniedError(
                "Недостаточно прав для управления получателями отчётов"
            )
        return context

    def require_warehouse_report_delivery(
        self, telegram_user_id: int
    ) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_send_warehouse_reports:
            raise AccessDeniedError("Недостаточно прав для отправки отчёта")
        return context

    def require_warehouse_delivery_history(
        self, telegram_user_id: int
    ) -> UserContext:
        context = self.get_context(telegram_user_id)
        if not context.can_view_warehouse_delivery_history:
            raise AccessDeniedError("Недостаточно прав для истории доставки")
        return context

    def can_assign_admin(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_assign_admin

    def can_assign_bartender(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_assign_bartender

    def can_assign_accountant(self, telegram_user_id: int) -> bool:
        return self.get_context(telegram_user_id).can_assign_accountant

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
                Role.ACCOUNTANT,
                Role.USER,
                Role.BLOCKED,
            }
        if target.role is Role.ADMIN or new_role is Role.ADMIN:
            return False
        return new_role in {
            Role.BARTENDER,
            Role.ACCOUNTANT,
            Role.USER,
            Role.BLOCKED,
        }

    def available_role_changes(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> list[Role]:
        target = self.get_context(target_user_id)
        return [
            role
            for role in (
                Role.ADMIN,
                Role.BARTENDER,
                Role.ACCOUNTANT,
                Role.USER,
                Role.BLOCKED,
            )
            if role is not target.role
            and self.can_change_role(actor_user_id, target_user_id, role)
        ]

    def require_role_change(
        self,
        actor_user_id: int,
        target_user_id: int,
        new_role: Role,
    ) -> UserContext:
        """Authorize one role-change attempt and audit a denial."""

        target = self.get_context(target_user_id)
        if self.can_change_role(actor_user_id, target_user_id, new_role):
            return target
        action = (
            "accountant_assignment_denied"
            if new_role is Role.ACCOUNTANT
            else "role_change_denied"
        )
        self._audit_role_change(
            actor_user_id,
            target_user_id,
            target.role,
            new_role,
            action,
            "error",
            "Изменение роли отклонено проверкой разрешений",
        )
        raise AccessDeniedError("Недостаточно прав")

    def change_role(
        self,
        actor_user_id: int,
        target_user_id: int,
        new_role: Role,
    ) -> UserRecord:
        """Recompute permissions immediately before the SQLite write."""

        self.require_role_change(actor_user_id, target_user_id, new_role)
        if self.user_service is None:
            raise RuntimeError("UserService is required for role changes")
        previous_role, updated = self.user_service.set_role(
            target_user_id,
            new_role,
            actor_user_id=actor_user_id,
        )
        if new_role is Role.ACCOUNTANT:
            action = "accountant_assigned"
            description = "Пользователю назначена роль бухгалтера"
        elif previous_role is Role.ACCOUNTANT:
            action = "accountant_removed"
            description = "С пользователя снята роль бухгалтера"
        elif new_role is Role.BLOCKED:
            action = "user_blocked"
            description = "Роль пользователя изменена"
        elif previous_role is Role.BLOCKED:
            action = "user_unblocked"
            description = "Роль пользователя изменена"
        else:
            action = "role_changed"
            description = "Роль пользователя изменена"
        self._audit_role_change(
            actor_user_id,
            target_user_id,
            previous_role,
            new_role,
            action,
            "success",
            description,
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
