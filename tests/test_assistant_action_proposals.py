from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.assistant_capabilities import CapabilityStatus
from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.proposals import (
    AssistantActionProposalService,
    AssistantProposalError,
    AssistantProposalExpired,
    AssistantProposalPermissionDenied,
)
from app.storage.assistant_action_database import AssistantActionDatabase
from app.storage.user_database import UserDatabase


class Access:
    def __init__(self, role: Role = Role.ADMIN) -> None:
        self.role = role

    def get_context(self, _actor: int):
        return type("Context", (), {"role": self.role})()


def _service(path: Path, now: list[datetime], access: Access | None = None):
    UserDatabase(path).initialize()
    return AssistantActionProposalService(
        AssistantActionDatabase(path),
        access or Access(),  # type: ignore[arg-type]
        CapabilityRegistry(),
        clock=lambda: now[0],
    )


def _proposal(service: AssistantActionProposalService, **changes):
    values = dict(
        actor_user_id=1,
        source_chat_id=10,
        source_message_id=20,
        capability_id="attendance.open_own",
        arguments={},
        preview={"title": "Открыть смену"},
    )
    values.update(changes)
    return service.create(**values)


def test_proposal_is_restart_safe_owned_and_callback_token_is_short(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    service = _service(tmp_path / "proposal.db", now)
    proposal = _proposal(service)
    assert len(f"ap:c:{proposal.token}".encode()) <= 64
    reopened = _service(tmp_path / "proposal.db", now)
    assert reopened.get_owned(proposal.token, 1, 10) == proposal
    assert reopened.get_owned(proposal.token, 2, 10) is None
    assert reopened.get_owned(proposal.token, 1, 11) is None


def test_confirm_is_idempotent_and_new_proposal_replaces_active(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    service = _service(tmp_path / "proposal.db", now)
    first = _proposal(service)
    second = _proposal(service, source_message_id=21)
    assert service.get_owned(first.token, 1, 10).status is CapabilityStatus.CANCELLED
    calls = []
    executed, result = service.confirm(
        second.token, 1, 10,
        lambda row: calls.append(row["capability_id"]) or {"ok": True},
    )
    assert executed.status is CapabilityStatus.EXECUTED
    replayed, replay_result = service.confirm(second.token, 1, 10, lambda _row: calls.append("duplicate"))
    assert replayed.status is CapabilityStatus.EXECUTED
    assert result == replay_result == {"ok": True}
    assert calls == ["attendance.open_own"]


def test_expiry_cancel_failure_and_role_change_are_safe(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    access = Access()
    service = _service(tmp_path / "proposal.db", now, access)
    expired = _proposal(service)
    now[0] += timedelta(minutes=11)
    with pytest.raises(AssistantProposalExpired):
        service.confirm(expired.token, 1, 10, lambda _row: {"ok": True})

    now[0] += timedelta(seconds=1)
    cancelled = _proposal(service)
    assert service.cancel(cancelled.token, 1, 10).status is CapabilityStatus.CANCELLED
    with pytest.raises(AssistantProposalError):
        service.confirm(cancelled.token, 1, 10, lambda _row: {"ok": True})

    failed = _proposal(service)
    with pytest.raises(RuntimeError):
        service.confirm(failed.token, 1, 10, lambda _row: (_ for _ in ()).throw(RuntimeError("boom")))
    assert service.get_owned(failed.token, 1, 10).status is CapabilityStatus.FAILED

    changed = _proposal(service)
    access.role = Role.ACCOUNTANT
    with pytest.raises(AssistantProposalPermissionDenied):
        service.confirm(changed.token, 1, 10, lambda _row: {"ok": True})


def test_high_risk_requires_reason_and_executor_rechecks_service_permission(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    service = _service(tmp_path / "proposal.db", now)
    with pytest.raises(AssistantProposalError):
        _proposal(
            service,
            capability_id="attendance.force_close",
            arguments={"target_ref": "session", "expected_revision": 1},
            target_id="session",
            target_revision=1,
        )
    proposal = _proposal(
        service,
        capability_id="attendance.force_close",
        arguments={"target_ref": "session", "expected_revision": 1, "reason": "забыл закрыть"},
        target_id="session",
        target_revision=1,
        reason="забыл закрыть",
    )
    with pytest.raises(AssistantProposalPermissionDenied):
        service.confirm(
            proposal.token, 1, 10,
            lambda _row: (_ for _ in ()).throw(AccessDeniedError("changed")),
        )
    assert service.get_owned(proposal.token, 1, 10).status is CapabilityStatus.CONFLICTED
