"""Restart-safe proposal lifecycle with ownership and role rechecks."""

from __future__ import annotations

import secrets
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.models.assistant_capabilities import (
    ActionProposal,
    CapabilityRisk,
    CapabilityStatus,
)
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.storage.assistant_action_database import AssistantActionDatabase
from app.storage.assistant_action_database import AssistantActionActiveConflict


class AssistantProposalError(RuntimeError):
    pass


class AssistantProposalExpired(AssistantProposalError):
    pass


class AssistantProposalPermissionDenied(AssistantProposalError):
    pass


class AssistantProposalTargetConflict(AssistantProposalError):
    pass


class AssistantProposalCollision(AssistantProposalError):
    """A new mutation cannot replace an unfinished proposal implicitly."""

    def __init__(self, active: ActionProposal) -> None:
        super().__init__("An unfinished proposal already exists")
        self.active = active


Executor = Callable[..., dict[str, Any]]


class AssistantActionProposalService:
    def __init__(
        self,
        repository: AssistantActionDatabase,
        access: AccessService,
        registry: CapabilityRegistry,
        access_policy: AssistantAccessPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int = 600,
        flags: dict[str, bool] | None = None,
    ) -> None:
        self.repository = repository
        self.access = access
        self.registry = registry
        self.access_policy = access_policy
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ttl_seconds = ttl_seconds
        self.flags = flags

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Proposal clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _public(row: dict[str, Any]) -> ActionProposal:
        return ActionProposal(
            token=str(row["opaque_token"]),
            capability_id=str(row["capability_id"]),
            risk_level=CapabilityRisk(int(row["risk_level"])),
            status=CapabilityStatus(str(row["status"])),
            target_type=row.get("target_type"),
            target_ref=row.get("target_id"),
            target_revision=row.get("target_revision"),
            arguments=dict(row["arguments"]),
            preview=dict(row["preview"]),
            reason=row.get("reason_text"),
            created_at_utc=str(row["created_at_utc"]),
            expires_at_utc=str(row["expires_at_utc"]),
            revision=int(row["revision"]),
        )

    def _authorize(self, actor_user_id: int, capability_id: str) -> None:
        self.access_policy.require_assistant_access(
            actor_user_id,
            chat_type="private",
        )
        capability = self.registry.require(capability_id)
        available = self.access_policy.available_capabilities(actor_user_id)
        if capability not in available:
            raise AssistantProposalPermissionDenied("Capability is no longer allowed")

    def create(
        self,
        *,
        actor_user_id: int,
        source_chat_id: int,
        source_message_id: int,
        capability_id: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        target_type: str | None = None,
        target_id: str | None = None,
        target_revision: int | None = None,
        reason: str | None = None,
    ) -> ActionProposal:
        capability = self.registry.require(capability_id)
        if capability.risk_level < CapabilityRisk.MUTATION:
            raise AssistantProposalError("Read and flow capabilities do not need proposals")
        self._authorize(actor_user_id, capability_id)
        validated = capability.input_model.model_validate(arguments).model_dump(mode="json")
        cleaned_reason = " ".join((reason or "").split()) or None
        if capability.confirmation_policy.value == "reason_and_explicit" and not cleaned_reason:
            raise AssistantProposalError("A reason is required")
        now = self._now()
        try:
            row = self.repository.create(
                {
                "opaque_token": secrets.token_urlsafe(12),
                "actor_user_id": actor_user_id,
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                "capability_id": capability_id,
                "risk_level": int(capability.risk_level),
                "target_type": target_type,
                "target_id": target_id,
                "target_revision": target_revision,
                "arguments": validated,
                "preview": preview,
                "reason_text": cleaned_reason,
                "created_at_utc": self._stamp(now),
                "expires_at_utc": self._stamp(now + timedelta(seconds=self.ttl_seconds)),
                },
                self._stamp(now),
            )
        except AssistantActionActiveConflict as exc:
            raise AssistantProposalCollision(self._public(exc.active)) from exc
        return self._public(row)

    def active_for_actor(self, actor_user_id: int) -> ActionProposal | None:
        row = self.repository.active_for_actor(
            actor_user_id, self._stamp(self._now())
        )
        return self._public(row) if row is not None else None

    def get_owned(self, token: str, actor_user_id: int, source_chat_id: int) -> ActionProposal | None:
        row = self.repository.get_owned(
            token, actor_user_id, source_chat_id, self._stamp(self._now())
        )
        return self._public(row) if row is not None else None

    def cancel(self, token: str, actor_user_id: int, source_chat_id: int) -> ActionProposal:
        self.access_policy.require_assistant_access(
            actor_user_id,
            chat_type="private",
        )
        current = self.get_owned(token, actor_user_id, source_chat_id)
        if current is None:
            raise AssistantProposalError("Proposal not found")
        if current.status is CapabilityStatus.CANCELLED:
            return current
        if current.status is CapabilityStatus.EXPIRED:
            raise AssistantProposalExpired("Proposal expired")
        if current.status is CapabilityStatus.EXECUTED:
            raise AssistantProposalError("Executed proposal cannot be cancelled")
        row = self.repository.transition(
            token, actor_user_id, source_chat_id,
            expected_status=current.status.value,
            status=CapabilityStatus.CANCELLED.value,
            now=self._stamp(self._now()),
        )
        return self._public(row)

    def confirm(
        self,
        token: str,
        actor_user_id: int,
        source_chat_id: int,
        executor: Executor,
    ) -> tuple[ActionProposal, dict[str, Any]]:
        current = self.get_owned(token, actor_user_id, source_chat_id)
        if current is None:
            raise AssistantProposalError("Proposal not found")
        if current.status is CapabilityStatus.EXPIRED:
            raise AssistantProposalExpired("Proposal expired")
        if current.status is CapabilityStatus.EXECUTED:
            row = self.repository.get_owned(
                token, actor_user_id, source_chat_id, self._stamp(self._now())
            )
            return current, dict((row or {}).get("result") or {})
        if current.status is not CapabilityStatus.READY:
            raise AssistantProposalError("Proposal is not ready")
        self._authorize(actor_user_id, current.capability_id)
        now = self._stamp(self._now())

        def run(
            confirmed: dict[str, Any],
            connection: sqlite3.Connection,
        ) -> dict[str, Any]:
            self._authorize(actor_user_id, current.capability_id)
            parameters = inspect.signature(executor).parameters
            if len(parameters) >= 2:
                return executor(confirmed, connection)
            return executor(confirmed)

        try:
            executed, result = self.repository.confirm_atomic(
                token,
                actor_user_id,
                source_chat_id,
                now=now,
                executor=run,
            )
        except AssistantProposalTargetConflict:
            self.repository.transition(
                token, actor_user_id, source_chat_id,
                expected_status=CapabilityStatus.READY.value,
                status=CapabilityStatus.CONFLICTED.value,
                now=now,
                result={"error": "target_conflict"},
            )
            raise
        except AccessDeniedError as exc:
            self.repository.transition(
                token, actor_user_id, source_chat_id,
                expected_status=CapabilityStatus.READY.value,
                status=CapabilityStatus.CONFLICTED.value,
                now=now,
                result={"error": "permission_denied"},
            )
            raise AssistantProposalPermissionDenied("Permission changed") from exc
        except AssistantProposalPermissionDenied:
            self.repository.transition(
                token, actor_user_id, source_chat_id,
                expected_status=CapabilityStatus.READY.value,
                status=CapabilityStatus.CONFLICTED.value,
                now=now,
                result={"error": "permission_denied"},
            )
            raise
        except Exception:
            self.repository.transition(
                token, actor_user_id, source_chat_id,
                expected_status=CapabilityStatus.READY.value,
                status=CapabilityStatus.FAILED.value,
                now=now,
                result={"error": "execution_failed"},
            )
            raise
        return self._public(executed), result
