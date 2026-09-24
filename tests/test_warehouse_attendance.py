from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.attendance_xlsx_service import WarehouseAttendanceXlsxService, parse_month
from app.services.warehouse.reminder_service import WarehouseReminderService, WarehouseReminderSettingsService
from app.storage.user_database import UserDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase, WarehouseAttendanceConflict
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase


class Clock:
    value=datetime(2026,9,24,15,30,tzinfo=UTC)
    def now(self): return self.value


def env(tmp_path: Path):
    path=tmp_path/"db.sqlite"; database=UserDatabase(path); database.initialize()
    users=UserService(database)
    for uid,name in ((10,"Admin"),(20,"Anna"),(21,"Boris"),(30,"Accountant")):
        users.ensure_user(uid,first_name=name)
    users.set_role(10,Role.ADMIN,actor_user_id=1); users.set_role(20,Role.BARTENDER,actor_user_id=10); users.set_role(21,Role.BARTENDER,actor_user_id=10); users.set_role(30,Role.ACCOUNTANT,actor_user_id=10)
    access=AuthService(frozenset(),users); repo=WarehouseAttendanceDatabase(path)
    return path,repo,WarehouseAttendanceService(repo,access,Clock()),access


def test_two_people_share_shift_and_close_independently(tmp_path: Path) -> None:
    path,repo,service,_=env(tmp_path)
    one,created=service.open(20); two,_=service.open(21)
    assert created and one["warehouse_shift_id"]==two["warehouse_shift_id"]
    same,created=service.open(20); assert not created and same["id"]==one["id"]
    closed,changed=service.close(20); assert changed and closed["status"]=="closed"
    assert repo.active_for_user(21) is not None
    assert service.close(20)[1] is False
    reopened,created=service.open(20); assert created and reopened["id"]!=one["id"]


def test_parallel_open_force_close_and_audit(tmp_path: Path) -> None:
    path,repo,service,_=env(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: service.open(20),range(2)))
    assert len({item[0]["id"] for item in results})==1
    row=repo.active_for_user(20); assert row
    forced=service.force_close(row["id"],10,"забыл закрыть",row["revision"])
    assert forced["status"]=="force_closed"
    with pytest.raises(WarehouseAttendanceConflict): service.force_close(row["id"],10,"again",row["revision"])


def test_timesheet_permissions_month_and_formula_safety(tmp_path: Path) -> None:
    path,repo,service,access=env(tmp_path); service.open(20); service.close(20)
    report=WarehouseAttendanceXlsxService(repo,access,tmp_path/"reports")
    generated=report.build_month(10,"2026-09"); assert generated.exists()
    with pytest.raises(PermissionError): report.build_month(20,"2026-09")
    with pytest.raises(ValueError): parse_month("09-2026")


def test_reminders_follow_active_participants_without_parallel_fallback(tmp_path: Path) -> None:
    import asyncio
    path,repo,attendance,access=env(tmp_path)
    attendance.open(20); attendance.open(21)
    settings=WarehouseReminderSettingsService(WarehouseReminderDatabase(path),access,Clock())
    source=int(repo.runtime_settings()["source_location_id"])
    settings.save(source,{"enabled":1,"target_chat_id":-100,"target_title":"Fallback",
                          "timezone":"Europe/Kyiv","first":30,"repeat":30,"maximum":3},0,10)
    class Sender:
        def __init__(self): self.recipients=[]
        async def send_reminder(self,**kwargs):
            self.recipients.append(kwargs["chat_id"]); return len(self.recipients)
    sender=Sender(); reminders=WarehouseReminderService(WarehouseReminderDatabase(path),sender,Clock())
    asyncio.run(reminders.run_once(datetime(2026,9,24,19,31,tzinfo=UTC)))
    assert sender.recipients == [20,21]


def test_closed_participant_is_excluded_from_next_occurrence(tmp_path: Path) -> None:
    import asyncio
    path,repo,attendance,access=env(tmp_path)
    attendance.open(20); attendance.open(21)
    settings=WarehouseReminderSettingsService(WarehouseReminderDatabase(path),access,Clock())
    source=int(repo.runtime_settings()["source_location_id"])
    settings.save(source,{"enabled":1,"target_chat_id":-100,"target_title":"Fallback",
                          "timezone":"Europe/Kyiv","first":30,"repeat":30,"maximum":3},0,10)
    class Sender:
        def __init__(self): self.recipients=[]
        async def send_reminder(self,**kwargs): self.recipients.append(kwargs["chat_id"]); return 1
    sender=Sender(); reminders=WarehouseReminderService(WarehouseReminderDatabase(path),sender,Clock())
    asyncio.run(reminders.run_once(datetime(2026,9,24,19,31,tzinfo=UTC)))
    attendance.close(20)
    asyncio.run(reminders.run_once(datetime(2026,9,24,20,1,tzinfo=UTC)))
    assert sender.recipients[-1] == 21
