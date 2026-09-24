"""Regressions for the final attendance-v12 integrity safeguards."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import app.handlers.warehouse_attendance as attendance_handlers
from app.handlers.warehouse_attendance import (
    _send_report,
    force_confirm,
    force_reason_prompt,
    force_reason_text,
    timesheet_period_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.attendance_xlsx_service import AttendanceReportCoordinator
from app.storage.user_database import UserDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 24, 15, 30, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def environment(tmp_path: Path):
    path = tmp_path / "attendance.sqlite"
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    for user_id, role, name in (
        (10, Role.ADMIN, "Admin"),
        (20, Role.BARTENDER, "Anna"),
        (21, Role.BARTENDER, "Boris"),
    ):
        users.ensure_user(user_id, first_name=name)
        users.set_role(user_id, role, actor_user_id=10)
    access = AuthService(frozenset(), users)
    clock = Clock()
    repository = WarehouseAttendanceDatabase(path)
    return path, access, clock, repository, WarehouseAttendanceService(
        repository, access, clock
    )


def state() -> FSMContext:
    return FSMContext(
        MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=10)
    )


def test_open_session_rejects_close_reason_on_insert_and_update(tmp_path: Path) -> None:
    path, _, _, _, service = environment(tmp_path)
    opened, created = service.open(20)
    assert created
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE warehouse_shift_sessions SET close_reason='invalid' WHERE id=?",
                (opened["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO warehouse_shift_sessions(
                       warehouse_shift_id,source_location_id,business_date,user_id,
                       opened_at_utc,opened_by_user_id,close_reason,status,
                       display_name_snapshot,created_at_utc,updated_at_utc,revision
                   ) VALUES(?,?,?,21,?,21,'invalid','open','Boris',?,?,1)""",
                (
                    opened["warehouse_shift_id"], opened["source_location_id"],
                    opened["business_date"], opened["opened_at_utc"],
                    opened["created_at_utc"], opened["updated_at_utc"],
                ),
            )


def test_normal_and_force_close_keep_v12_integrity(tmp_path: Path) -> None:
    path, _, clock, _, service = environment(tmp_path)
    service.open(20)
    clock.value = datetime(2026, 9, 24, 16, 30, tzinfo=UTC)
    normal, changed = service.close(20)
    assert changed and normal is not None and normal["close_reason"] is None
    forced_target, _ = service.open(21)
    clock.value = datetime(2026, 9, 24, 17, 30, tzinfo=UTC)
    forced = service.force_close(
        forced_target["id"], 10, "Forgot to close", forced_target["revision"]
    )
    assert forced["close_reason"] == "Forgot to close"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_report_tokens_are_one_shot_and_user_bound() -> None:
    coordinator = AttendanceReportCoordinator(completed_ttl_seconds=0)
    coordinator.issue(1, token=11)
    assert coordinator.claim(1, "2026-09", 11)
    assert not coordinator.claim(1, "2026-09", 11)
    assert not coordinator.claim(2, "2026-09", 11)
    coordinator.finish(1, "2026-09", 11)
    assert not coordinator.claim(1, "2026-09", 11)

    coordinator.issue(1, token=12)
    assert not coordinator.claim(1, "2026-09", 11)
    assert coordinator.claim(1, "2026-09", 12)
    coordinator.finish(1, "2026-09", 12)
    assert not coordinator.claim(1, "2026-09", 11)
    assert not coordinator.claim(1, "2026-09", 12)


def test_report_failure_needs_new_token_and_periods_do_not_conflict() -> None:
    coordinator = AttendanceReportCoordinator()
    coordinator.issue(1, token=21)
    assert coordinator.claim(1, "2026-09", 21)
    coordinator.release(1, "2026-09", 21)
    assert not coordinator.claim(1, "2026-09", 21)

    coordinator.issue(1, token=22)
    assert coordinator.claim(1, "2026-09", 22)
    coordinator.issue(1, token=23)
    assert coordinator.claim(1, "2026-08", 23)
    coordinator.issue(2, token=24)
    assert coordinator.claim(2, "2026-09", 24)


def test_concurrent_duplicate_consumes_losing_token() -> None:
    coordinator = AttendanceReportCoordinator()
    coordinator.issue(1, token=25)
    assert coordinator.claim(1, "2026-09", 25)
    coordinator.issue(1, token=26)
    assert not coordinator.claim(1, "2026-09", 26)
    coordinator.finish(1, "2026-09", 25)
    assert not coordinator.claim(1, "2026-09", 26)


def test_newly_issued_token_invalidates_previous_unconsumed_token() -> None:
    coordinator = AttendanceReportCoordinator()
    coordinator.issue(1, token=31)
    coordinator.issue(1, token=32)
    assert not coordinator.claim(1, "2026-09", 31)
    assert coordinator.claim(1, "2026-09", 32)
    assert not AttendanceReportCoordinator().claim(1, "2026-09", 32)


def test_cancellation_waits_for_generator_cleans_file_and_releases_claim(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    generated = tmp_path / "cancelled.xlsx"

    class SlowService:
        def build_month(self, actor_user_id: int, period: str) -> Path:
            started.set()
            release.wait(timeout=5)
            generated.write_bytes(b"synthetic")
            return generated

    async def scenario() -> None:
        coordinator = AttendanceReportCoordinator()
        coordinator.issue(10, token=41)
        callback = SimpleNamespace(from_user=SimpleNamespace(id=10), message=None)
        task = asyncio.create_task(
            _send_report(callback, SlowService(), coordinator, "2026-09", 41)
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not generated.exists()
        assert not coordinator.claim(10, "2026-09", 41)
        coordinator.issue(10, token=42)
        assert coordinator.claim(10, "2026-09", 42)

    asyncio.run(scenario())


def test_cancellation_during_send_cleans_file_and_allows_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tmp_path / "during-send.xlsx"
    send_started = asyncio.Event()
    finish_send = asyncio.Event()

    class Service:
        def build_month(self, actor_user_id: int, period: str) -> Path:
            generated.write_bytes(b"synthetic")
            return generated

    class FakeMessage:
        async def answer_document(self, *args, **kwargs) -> None:
            send_started.set()
            await finish_send.wait()

    async def scenario() -> None:
        monkeypatch.setattr(attendance_handlers, "Message", FakeMessage)
        coordinator = AttendanceReportCoordinator()
        coordinator.issue(10, token=51)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10), message=FakeMessage()
        )
        task = asyncio.create_task(
            _send_report(callback, Service(), coordinator, "2026-09", 51)
        )
        await send_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish_send.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not generated.exists()
        assert not coordinator.claim(10, "2026-09", 51)
        coordinator.issue(10, token=52)
        assert coordinator.claim(10, "2026-09", 52)

    asyncio.run(scenario())


def test_stale_token_never_builds_or_sends_and_callbacks_fit_limit() -> None:
    class Service:
        build_month = Mock()

    async def scenario() -> None:
        coordinator = AttendanceReportCoordinator()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=10), message=None)
        assert not await _send_report(
            callback, Service(), coordinator, "2026-09", 99
        )
        Service.build_month.assert_not_called()

    asyncio.run(scenario())
    keyboard = timesheet_period_keyboard((1 << 63) - 1)
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data is not None
    )


def test_new_force_target_replaces_reason_and_requires_new_reason() -> None:
    async def scenario() -> None:
        fsm = state()
        await fsm.update_data(
            session_id=1, revision=1, target_user_id=20,
            request_token=61, reason="reason A",
        )
        target = {
            "id": 2, "revision": 3, "user_id": 21,
            "display_name_snapshot": "Boris", "username_snapshot": "boris",
            "source_location_name": "Bar", "business_date": "2026-09-24",
            "opened_at_utc": "2026-09-24T10:00:00Z",
        }
        force_close = Mock(return_value={})
        service = SimpleNamespace(
            open_sessions=lambda actor_user_id: [target],
            duration_seconds=lambda row, now: 3600,
            clock=SimpleNamespace(now=lambda: datetime(2026, 9, 24, 11, tzinfo=UTC)),
            force_close=force_close,
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10), answer=AsyncMock(), message=None
        )
        await force_reason_prompt(
            callback, WarehouseCallback(action="ar", object_id=2, aux=3),
            fsm, SimpleNamespace(), service, True,
        )
        data = await fsm.get_data()
        assert "reason" not in data
        await force_confirm(
            callback,
            WarehouseCallback(
                action="aF", object_id=2, aux=int(data["request_token"])
            ),
            fsm, SimpleNamespace(), service, True,
        )
        force_close.assert_not_called()

        auth = SimpleNamespace(
            get_context=lambda user_id: SimpleNamespace(
                can_manage_warehouse_attendance=True
            )
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10), text="reason B", answer=AsyncMock()
        )
        await force_reason_text(message, fsm, auth)
        data = await fsm.get_data()
        await force_confirm(
            callback,
            WarehouseCallback(
                action="aF", object_id=2, aux=int(data["request_token"])
            ),
            fsm, auth, service, True,
        )
        force_close.assert_called_once_with(2, 10, "reason B", 3)
        assert await fsm.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "imports",
    [
        "from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase",
        "from app.storage.warehouse_operations_database import WarehouseOperationsDatabase",
        "from app.services.warehouse.attendance_service import WarehouseAttendanceService",
        "from app.bot import main",
        (
            "from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase; "
            "from app.storage.warehouse_operations_database import WarehouseOperationsDatabase; "
            "from app.services.warehouse.attendance_service import WarehouseAttendanceService; "
            "from app.bot import main"
        ),
        (
            "from app.bot import main; "
            "from app.services.warehouse.attendance_service import WarehouseAttendanceService; "
            "from app.storage.warehouse_operations_database import WarehouseOperationsDatabase; "
            "from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase"
        ),
    ],
)
def test_warehouse_imports_work_in_clean_subprocess(imports: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", imports], cwd=ROOT,
        text=True, capture_output=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
