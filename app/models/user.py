"""User and role models for the local BarBot instance."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    BARTENDER = "bartender"
    ACCOUNTANT = "accountant"
    USER = "user"
    BLOCKED = "blocked"


DATABASE_ROLES = frozenset(
    {Role.ADMIN, Role.BARTENDER, Role.ACCOUNTANT, Role.USER, Role.BLOCKED}
)


class UserRecord(BaseModel):
    """A user persisted in the restaurant-local SQLite database."""

    model_config = ConfigDict(extra="forbid")

    telegram_user_id: int = Field(gt=0)
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    role: Role = Role.USER
    is_active: bool = True
    first_seen_at: str
    last_seen_at: str
    created_by: int | None = None
    updated_at: str
    has_started: bool = False

    @field_validator("role")
    @classmethod
    def role_must_be_storable(cls, role: Role) -> Role:
        if role not in DATABASE_ROLES:
            raise ValueError("super_admin is configured through the environment")
        return role

    @property
    def display_name(self) -> str:
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts) or "Имя не указано"
