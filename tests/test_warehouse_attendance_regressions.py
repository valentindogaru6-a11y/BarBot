import asyncio
import sqlite3
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery

from app.models.user import Role
from app.handlers.warehouse_attendance import force_cancel, force_confirm
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.attendance_xlsx_service import (
    AttendanceReportCoordinator, WarehouseAttendanceXlsxService, _duration,
)
from app.services.warehouse.reminder_service import (
    WarehouseReminderService, WarehouseReminderSettingsService,
)
from app.storage.user_database import UserDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase


class Clock:
    def __init__(self, value=datetime(2026, 9, 24, 15, 30, tzinfo=UTC)):
        self.value = value

    def now(self):
        return self.value


def environment(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    database = UserDatabase(path); database.initialize()
    users = UserService(database)
    for user_id, role, name in (
        (10, Role.ADMIN, "Admin"), (20, Role.BARTENDER, "Anna"),
        (21, Role.BARTENDER, "Boris"),
    ):
        users.ensure_user(user_id, first_name=name)
        users.set_role(user_id, role, actor_user_id=10)
    access = AuthService(frozenset(), users); clock = Clock()
    repository = WarehouseAttendanceDatabase(path)
    return path, access, clock, repository, WarehouseAttendanceService(repository, access, clock)


def _shared_and_calendar(path: Path):
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in item.findall(".//a:t", ns)) for item in root.findall("a:si", ns)]
        calendar = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
    def value(ref: str):
        cell = calendar.find(f".//a:c[@r='{ref}']", ns)
        node = cell.find("a:v", ns) if cell is not None else None
        return strings[int(node.text)] if cell is not None and cell.get("t") == "s" and node is not None else None
    return value


@pytest.mark.parametrize("closed_count", [1, 2])
def test_calendar_keeps_closed_time_when_same_day_is_open(tmp_path: Path, closed_count: int) -> None:
    _, access, clock, repository, service = environment(tmp_path)
    for _ in range(closed_count):
        service.open(20); clock.value = clock.value.replace(hour=clock.value.hour + 2); service.close(20)
    service.open(20)
    report = WarehouseAttendanceXlsxService(repository, access, tmp_path / "reports").build_month(10, "2026-09")
    value = _shared_and_calendar(report)
    assert value("Y2") == f"{closed_count * 2} ч 00 мин + ОТКРЫТА"
    assert value("AF2") == f"{closed_count * 2} ч 00 мин"


def test_force_closed_time_is_kept_with_open_session(tmp_path: Path) -> None:
    _, access, clock, repository, service = environment(tmp_path)
    row, _ = service.open(20); clock.value = clock.value.replace(hour=17, minute=30)
    service.force_close(row["id"], 10, "reason", row["revision"]); service.open(20)
    report = WarehouseAttendanceXlsxService(repository, access, tmp_path / "reports").build_month(10, "2026-09")
    assert _shared_and_calendar(report)("Y2") == "2 ч 00 мин + ОТКРЫТА"


@pytest.mark.parametrize("business_date,opened,closed", [
    ("2026-02-30", "2026-09-24T10:00:00Z", None),
    ("2026-09-24", "not-a-time", None),
    ("2026-09-24", "2026-09-24T10:00:00+03:00", None),
    ("2026-09-24", "2026-09-24T10:00:00Z", "2026-09-24T09:00:00Z"),
])
def test_sqlite_rejects_invalid_attendance_time(tmp_path: Path, business_date, opened, closed) -> None:
    path, _, _, repository, service = environment(tmp_path); row, _ = service.open(20)
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO warehouse_shift_sessions(warehouse_shift_id,source_location_id,
               business_date,user_id,opened_at_utc,closed_at_utc,opened_by_user_id,
               closed_by_user_id,status,display_name_snapshot,created_at_utc,updated_at_utc,revision)
               VALUES(?,?,?,?,?,?,?,?,?,'Invalid','2026-09-24T10:00:00Z','2026-09-24T10:00:00Z',1)""",
            (row["warehouse_shift_id"], row["source_location_id"], business_date, 21,
             opened, closed, 21, 21 if closed else None, "closed" if closed else "open"),
        )


def test_duration_rejects_negative_interval() -> None:
    with pytest.raises(ValueError, match="precedes"):
        _duration({"opened_at_utc": "2026-09-24T10:00:00Z", "closed_at_utc": "2026-09-24T09:00:00Z"})


def test_reminder_restart_reclaims_stale_personal_delivery(tmp_path: Path) -> None:
    path, access, clock, repository, service = environment(tmp_path); session, _ = service.open(20)
    reminders = WarehouseReminderDatabase(path); source = repository.runtime_settings()["source_location_id"]
    WarehouseReminderSettingsService(reminders, access, clock).save(source, {
        "enabled": 1, "target_chat_id": -1, "target_title": "Fallback",
        "timezone": "Europe/Kyiv", "first": 30, "repeat": 30, "maximum": 1,
    }, 0, 10)
    reminders.ensure_events(session["warehouse_shift_id"], [(1, "2026-09-24T19:30:00Z")], "2026-09-24T15:00:00Z")
    event = reminders.due("2026-09-24T19:31:00Z", "2026-09-24T19:16:00Z")[0]
    reminders.claim(event["id"], "2026-09-24T19:31:00Z")
    reminders.claim_delivery(event["id"], 20, "2026-09-24T19:31:00Z")
    class Sender:
        calls = 0
        async def send_reminder(self, **kwargs): self.calls += 1; return 7
    sender = Sender()
    asyncio.run(WarehouseReminderService(reminders, sender, clock).run_once(datetime(2026, 9, 24, 19, 47, tzinfo=UTC)))
    assert sender.calls == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT status FROM warehouse_reminder_events WHERE id=?", (event["id"],)).fetchone()[0] == "sent"
        assert connection.execute("SELECT status FROM warehouse_reminder_event_deliveries WHERE event_id=?", (event["id"],)).fetchone()[0] == "sent"


def test_recipients_follow_source_location_across_cutoff(tmp_path: Path) -> None:
    path, _, clock, repository, service = environment(tmp_path); old, _ = service.open(20)
    with sqlite3.connect(path) as connection:
        location = old["source_location_id"]
        new_shift = connection.execute(
            """INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,
               cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,revision,
               created_at_utc,updated_at_utc) VALUES(?,'2026-09-25','Europe/Kyiv','23:00',
               'open','2026-09-24T20:00:00Z','2026-09-25T20:00:00Z',1,
               '2026-09-24T20:00:00Z','2026-09-24T20:00:00Z')""", (location,)
        ).lastrowid; connection.commit()
        other_location = connection.execute(
            """INSERT INTO warehouse_locations(stable_id,name,normalized_name,is_active,
               can_be_source,can_be_destination,created_at_utc,updated_at_utc,revision)
               VALUES('other','Other','other',1,1,1,'x','x',1)"""
        ).lastrowid
        other_shift = connection.execute(
            """INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,
               cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,revision,
               created_at_utc,updated_at_utc) VALUES(?,'2026-09-25','Europe/Kyiv','23:00',
               'open','2026-09-24T20:00:00Z','2026-09-25T20:00:00Z',1,'x','x')""",
            (other_location,),
        ).lastrowid
        connection.execute(
            """INSERT INTO warehouse_shift_sessions(warehouse_shift_id,source_location_id,
               business_date,user_id,opened_at_utc,opened_by_user_id,status,
               display_name_snapshot,created_at_utc,updated_at_utc,revision)
               VALUES(?,?,'2026-09-25',21,'2026-09-24T20:00:00Z',21,'open','Boris',
               '2026-09-24T20:00:00Z','2026-09-24T20:00:00Z',1)""",
            (other_shift, other_location),
        ); connection.commit()
    assert WarehouseReminderDatabase(path).active_recipients(new_shift) == [20]


def test_business_today_uses_cutoff(tmp_path: Path) -> None:
    _, _, clock, _, service = environment(tmp_path)
    clock.value = datetime(2026, 9, 24, 19, 59, tzinfo=UTC)
    assert service.current_period(20).business_date.isoformat() == "2026-09-24"
    clock.value = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    assert service.current_period(20).business_date.isoformat() == "2026-09-25"


def test_report_coordinator_is_token_and_period_idempotent() -> None:
    coordinator = AttendanceReportCoordinator()
    coordinator.issue(1, token=11)
    assert coordinator.claim(1, "2026-09", 11)
    assert not coordinator.claim(1, "2026-09", 11)
    coordinator.finish(1, "2026-09", 11)
    assert not coordinator.claim(1, "2026-09", 11)
    coordinator.issue(1, token=12)
    assert coordinator.claim(1, "2026-08", 12)
    coordinator.issue(2, token=11)
    assert coordinator.claim(2, "2026-09", 11)
    coordinator.release(1, "2026-08", 12)
    assert not coordinator.claim(1, "2026-08", 12)
    coordinator.issue(1, token=13)
    assert coordinator.claim(1, "2026-08", 13)


def _state() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=10))


def test_force_confirm_rejects_callback_for_other_fsm_target() -> None:
    state = _state(); asyncio.run(state.update_data(session_id=2, revision=4, request_token=22, reason="B"))
    callback = SimpleNamespace(from_user=SimpleNamespace(id=10), answer=AsyncMock(), message=None)
    service = SimpleNamespace(force_close=AsyncMock())
    asyncio.run(force_confirm(callback, WarehouseCallback(action="aF", object_id=1, aux=11), state,
                              SimpleNamespace(), service, True))
    service.force_close.assert_not_awaited(); assert asyncio.run(state.get_data())["session_id"] == 2


def test_force_cancel_clears_matching_state() -> None:
    state = _state(); asyncio.run(state.update_data(session_id=2, request_token=22, reason="B"))
    callback = SimpleNamespace(answer=AsyncMock(), message=None)
    asyncio.run(force_cancel(callback, WarehouseCallback(action="aX", aux=22), state))
    assert asyncio.run(state.get_data()) == {}


def test_expired_force_callback_stops_before_business_method() -> None:
    state = _state(); asyncio.run(state.update_data(session_id=2, revision=4, request_token=22, reason="B"))
    error = TelegramBadRequest(AnswerCallbackQuery(callback_query_id="expired"), "query is too old")
    callback = SimpleNamespace(from_user=SimpleNamespace(id=10), answer=AsyncMock(side_effect=error), message=None)
    service = SimpleNamespace(force_close=AsyncMock())
    asyncio.run(force_confirm(callback, WarehouseCallback(action="aF", object_id=2, aux=22), state,
                              SimpleNamespace(), service, True))
    service.force_close.assert_not_awaited()
