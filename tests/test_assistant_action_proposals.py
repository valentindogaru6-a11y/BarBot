from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from app.models.assistant_capabilities import CapabilityStatus
from app.models.user import Role
from app.config import Settings
from app.services.access_service import AccessDeniedError
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.capabilities import CapabilityBinding
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.services.assistant.proposals import (
    AssistantActionProposalService,
    AssistantProposalError,
    AssistantProposalExpired,
    AssistantProposalPermissionDenied,
)
from app.storage.assistant_action_database import AssistantActionDatabase
from app.storage.user_database import UserDatabase
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase


class Access:
    def __init__(self, role: Role = Role.ADMIN) -> None:
        self.role = role

    def get_context(self, _actor: int):
        return type(
            "Context",
            (),
            {
                "role": self.role,
                "user": type("User", (), {"is_active": True})(),
                "can_use_warehouse_attendance": self.role
                in {Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER},
                "can_manage_warehouse_attendance": self.role
                in {Role.SUPER_ADMIN, Role.ADMIN},
            },
        )()


def _service(path: Path, now: list[datetime], access: Access | None = None):
    UserDatabase(path).initialize()
    registry = CapabilityRegistry()
    adapter = object()
    executor = lambda: None
    registry.bind({
        capability_id: CapabilityBinding(
            capability_id,
            adapter,
            adapter,
            "test route",
            "test renderer",
            executor,
        )
        for capability_id in ("attendance.open_own", "attendance.force_close")
    })
    policy = AssistantAccessPolicy(
        access or Access(),  # type: ignore[arg-type]
        registry,
        Settings(
            _env_file=None,
            bot_token="test",
            ai_assistant_enabled=True,
            ai_access_mode="role_based",
            warehouse_enabled=True,
        ),
    )
    return AssistantActionProposalService(
        AssistantActionDatabase(path),
        access or Access(),  # type: ignore[arg-type]
        registry,
        policy,
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


def test_confirm_is_idempotent_and_new_proposal_requires_explicit_resolution(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    service = _service(tmp_path / "proposal.db", now)
    first = _proposal(service)
    from app.services.assistant.proposals import AssistantProposalCollision
    with pytest.raises(AssistantProposalCollision) as collision:
        _proposal(service, source_message_id=21)
    assert collision.value.active.token == first.token
    assert service.get_owned(first.token, 1, 10).status is CapabilityStatus.READY
    service.cancel(first.token, 1, 10)
    second = _proposal(service, source_message_id=21)
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


def test_concurrent_confirmation_executes_once(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    path = tmp_path / "concurrent-proposal.db"
    service = _service(path, now)
    proposal = _proposal(service)
    entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def executor(_row, _connection):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        time.sleep(0.1)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.confirm, proposal.token, 1, 10, executor)
        assert entered.wait(timeout=2)
        second = pool.submit(service.confirm, proposal.token, 1, 10, executor)
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert calls == 1
    assert first_result[1] == second_result[1] == {"ok": True}
    assert first_result[0].status is CapabilityStatus.EXECUTED
    assert second_result[0].status is CapabilityStatus.EXECUTED


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


def test_pilot_access_is_rechecked_before_confirm_and_cancel(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    access = Access(Role.SUPER_ADMIN)
    path = tmp_path / "pilot-recheck.db"
    UserDatabase(path).initialize()
    registry = CapabilityRegistry()
    marker = object()
    registry.bind({
        "attendance.open_own": CapabilityBinding(
            "attendance.open_own", marker, marker, "route", "renderer", lambda: None
        )
    })
    settings = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="pilot_allowlist",
        ai_pilot_user_ids="1",
        warehouse_enabled=True,
    )
    policy = AssistantAccessPolicy(access, registry, settings)  # type: ignore[arg-type]
    service = AssistantActionProposalService(
        AssistantActionDatabase(path),
        access,  # type: ignore[arg-type]
        registry,
        policy,
        clock=lambda: now[0],
    )
    proposal = _proposal(service)
    calls = []

    settings.ai_pilot_user_ids = frozenset()
    with pytest.raises(AccessDeniedError):
        _proposal(service, actor_user_id=2, source_chat_id=20)
    with pytest.raises(AccessDeniedError):
        service.confirm(
            proposal.token,
            1,
            10,
            lambda _row: calls.append("mutation") or {"ok": True},
        )
    with pytest.raises(AccessDeniedError):
        service.cancel(proposal.token, 1, 10)
    assert calls == []

    settings.ai_pilot_user_ids = frozenset({1})
    settings.ai_assistant_enabled = False
    with pytest.raises(AccessDeniedError):
        service.confirm(proposal.token, 1, 10, lambda _row: {"ok": True})


def test_access_mode_change_and_blocking_prevent_confirmation(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    access = Access(Role.ADMIN)
    service = _service(tmp_path / "mode-recheck.db", now, access)
    proposal = _proposal(service)
    settings = service.access_policy.settings

    settings.ai_access_mode = "pilot_allowlist"
    settings.ai_pilot_user_ids = frozenset({1})
    with pytest.raises(AccessDeniedError):
        service.confirm(proposal.token, 1, 10, lambda _row: {"ok": True})

    settings.ai_access_mode = "role_based"
    access.role = Role.BLOCKED
    with pytest.raises(AccessDeniedError):
        service.confirm(proposal.token, 1, 10, lambda _row: {"ok": True})


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


def test_business_mutation_audit_and_proposal_result_rollback_together(
    tmp_path: Path,
) -> None:
    path = tmp_path / "atomic.db"
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    users.ensure_user(10, first_name="Bartender")
    users.set_role(10, Role.BARTENDER, actor_user_id=10)
    access = AuthService(frozenset(), users)
    registry = CapabilityRegistry()
    marker = object()
    executor = lambda: None
    registry.bind({
        "attendance.open_own": CapabilityBinding(
            "attendance.open_own", marker, marker, "route", "renderer", executor
        )
    })
    settings = Settings(
        _env_file=None, bot_token="test", ai_assistant_enabled=True,
        ai_access_mode="role_based", warehouse_enabled=True,
    )
    policy = AssistantAccessPolicy(access, registry, settings)
    proposals = AssistantActionProposalService(
        AssistantActionDatabase(path), access, registry, policy
    )
    attendance = WarehouseAttendanceService(
        WarehouseAttendanceDatabase(path), access
    )
    proposal = proposals.create(
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
        capability_id="attendance.open_own",
        arguments={},
        preview={"title": "Открыть смену"},
    )

    def mutate_then_fail(row, connection):
        del row
        attendance.open(10, connection)
        raise RuntimeError("result serialization failed")

    with pytest.raises(RuntimeError):
        proposals.confirm(proposal.token, 10, 10, mutate_then_fail)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_shift_sessions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_audit_events WHERE event_type='warehouse_attendance_opened'"
        ).fetchone()[0] == 0
    assert proposals.get_owned(proposal.token, 10, 10).status is CapabilityStatus.FAILED

    retry = proposals.create(
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=2,
        capability_id="attendance.open_own",
        arguments={},
        preview={"title": "Открыть смену"},
    )
    executed, result = proposals.confirm(
        retry.token,
        10,
        10,
        lambda row, connection: {
            "created": attendance.open(10, connection)[1],
            "capability": row["capability_id"],
        },
    )
    assert executed.status is CapabilityStatus.EXECUTED
    assert result == {"created": True, "capability": "attendance.open_own"}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_shift_sessions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_audit_events WHERE event_type='warehouse_attendance_opened'"
        ).fetchone()[0] == 1


def test_reminder_mutation_uses_the_same_proposal_transaction(tmp_path: Path) -> None:
    path = tmp_path / "reminder-atomic.db"
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    users.ensure_user(10, first_name="Admin")
    users.set_role(10, Role.ADMIN, actor_user_id=10)
    access = AuthService(frozenset(), users)
    registry = CapabilityRegistry()
    marker = object()
    executor = lambda: None
    registry.bind({
        "reminders.update": CapabilityBinding(
            "reminders.update", marker, marker, "route", "renderer", executor
        )
    })
    settings = Settings(
        _env_file=None, bot_token="test", ai_assistant_enabled=True,
        ai_access_mode="role_based", warehouse_enabled=True,
    )
    policy = AssistantAccessPolicy(access, registry, settings)
    proposals = AssistantActionProposalService(
        AssistantActionDatabase(path), access, registry, policy,
        flags={"ai_assistant_enabled": True, "warehouse_enabled": True},
    )
    reminders = WarehouseReminderSettingsService(
        WarehouseReminderDatabase(path), access
    )
    with sqlite3.connect(path) as connection:
        source_id = int(connection.execute(
            "SELECT default_source_location_id FROM warehouse_settings WHERE id=1"
        ).fetchone()[0])
    proposal = proposals.create(
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
        capability_id="reminders.update",
        arguments={
            "target_ref": str(source_id),
            "expected_revision": 0,
            "values": {"first": 30},
        },
        preview={"before": 60, "after": 30},
        target_type="reminder_settings",
        target_id=str(source_id),
        target_revision=0,
    )

    def mutate_then_fail(row, connection):
        reminders.update(
            source_id,
            dict(row["arguments"]["values"]),
            0,
            10,
            connection,
        )
        raise RuntimeError("proposal result failed")

    with pytest.raises(RuntimeError):
        proposals.confirm(proposal.token, 10, 10, mutate_then_fail)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reminder_settings"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_audit_events WHERE event_type LIKE 'warehouse_reminder_%'"
        ).fetchone()[0] == 0
