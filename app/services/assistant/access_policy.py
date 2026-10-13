"""Fail-closed entry policy for the optional AI assistant surface."""

from __future__ import annotations

from app.config import Settings
from app.models.user import Role
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
)


PILOT_DENIED_TEXT = "AI-помощник пока доступен только в закрытом пилоте."
_ROLE_BASED_AI_ROLES = frozenset(
    {Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER, Role.ACCOUNTANT}
)


class AssistantAccessDenied(AccessDeniedError):
    """The actor is outside the configured assistant entry boundary."""


class AssistantAccessPolicy:
    """Separate assistant entry eligibility from per-capability authority."""

    def __init__(
        self,
        access: AccessService,
        registry: CapabilityRegistry,
        settings: Settings,
    ) -> None:
        self.access = access
        self.registry = registry
        self.settings = settings

    @property
    def capability_flags(self) -> dict[str, bool]:
        return {
            "ai_assistant_enabled": self.settings.ai_assistant_enabled,
            "warehouse_enabled": self.settings.warehouse_enabled,
        }

    def can_enter_assistant(
        self,
        actor_user_id: int,
        *,
        chat_type: str,
    ) -> bool:
        if not self.settings.ai_assistant_enabled or chat_type != "private":
            return False
        context = self.access.get_context(actor_user_id)
        if (
            context.user is None
            or not context.user.is_active
            or context.role in {Role.USER, Role.BLOCKED}
        ):
            return False
        if self.settings.ai_access_mode == "pilot_allowlist":
            return bool(
                context.role is Role.SUPER_ADMIN
                and actor_user_id in self.settings.ai_pilot_user_ids
            )
        if self.settings.ai_access_mode == "role_based":
            return context.role in _ROLE_BASED_AI_ROLES
        return False

    def require_assistant_access(
        self,
        actor_user_id: int,
        *,
        chat_type: str,
    ) -> None:
        if not self.can_enter_assistant(actor_user_id, chat_type=chat_type):
            raise AssistantAccessDenied(PILOT_DENIED_TEXT)

    def available_capabilities(
        self,
        actor_user_id: int,
        *,
        chat_type: str = "private",
    ) -> tuple[CapabilityDefinition, ...]:
        if not self.can_enter_assistant(actor_user_id, chat_type=chat_type):
            return ()
        return self.registry.available_for_actor(
            self.access,
            actor_user_id,
            chat_type=chat_type,
            flags=self.capability_flags,
        )


__all__ = [
    "AssistantAccessDenied",
    "AssistantAccessPolicy",
    "PILOT_DENIED_TEXT",
]
