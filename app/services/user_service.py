"""Application service for user registration and local lookup."""

from aiogram.types import User

from app.models.user import Role, UserRecord
from app.services.audit_service import AuditService
from app.storage.user_database import UserDatabase


class UserService:
    def __init__(
        self,
        database: UserDatabase,
        audit: AuditService | None = None,
    ) -> None:
        self.database = database
        self.audit = audit

    def sync_telegram_user(self, user: User) -> UserRecord:
        existing = self.database.get_user(user.id)
        record = self.database.sync_user(
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        if existing is None:
            self._audit_registration(user.id, user.id)
        return record

    def ensure_user(self, telegram_user_id: int, **profile) -> UserRecord:
        existing = self.database.get_user(telegram_user_id)
        record = self.database.ensure_user(telegram_user_id, **profile)
        if existing is None:
            self._audit_registration(
                int(profile.get("created_by") or telegram_user_id),
                telegram_user_id,
            )
        return record

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return self.database.get_user(telegram_user_id)

    def list_users(
        self,
        *,
        role: Role | None = None,
        page: int = 0,
        page_size: int = 8,
    ) -> tuple[list[UserRecord], int, int]:
        count = self.database.count_users(role)
        total_pages = max(1, (count + page_size - 1) // page_size)
        safe_page = min(max(page, 0), total_pages - 1)
        users = self.database.list_users(
            role=role,
            limit=page_size,
            offset=safe_page * page_size,
        )
        return users, safe_page, total_pages

    def search_users(self, query: str) -> list[UserRecord]:
        return self.database.search_users(query)

    def count_by_role(self) -> dict[Role, int]:
        return {
            role: self.database.count_users(role)
            for role in (Role.ADMIN, Role.BARTENDER, Role.USER, Role.BLOCKED)
        }

    def set_role(
        self,
        telegram_user_id: int,
        role: Role,
        *,
        actor_user_id: int,
    ) -> tuple[Role, UserRecord]:
        return self.database.set_role(
            telegram_user_id,
            role,
            actor_user_id=actor_user_id,
        )

    def block_user(
        self,
        telegram_user_id: int,
        *,
        actor_user_id: int,
    ) -> tuple[Role, UserRecord]:
        return self.set_role(
            telegram_user_id,
            Role.BLOCKED,
            actor_user_id=actor_user_id,
        )

    def unblock_user(
        self,
        telegram_user_id: int,
        *,
        actor_user_id: int,
    ) -> tuple[Role, UserRecord]:
        return self.set_role(
            telegram_user_id,
            Role.USER,
            actor_user_id=actor_user_id,
        )

    def _audit_registration(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> None:
        if self.audit is not None:
            self.audit.record_user_event(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                previous_role="",
                new_role=Role.USER.value,
                action="user_registered",
                status="success",
                description="Пользователь зарегистрирован в локальной базе",
            )
