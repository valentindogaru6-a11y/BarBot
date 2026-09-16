import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.access_service import AccessDeniedError, AccessService
from app.bot import start_reminder_worker, stop_reminder_worker
from app.keyboards.warehouse import reminder_settings_keyboard
from app.services.warehouse.reminder_service import (
    WarehouseReminderService,
    WarehouseReminderSettingsService,
)
from app.storage.user_database import UserDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderConflict, WarehouseReminderDatabase
from app.storage.warehouse_reminder_schema import validate_warehouse_reminder_schema


class Clock:
    def __init__(self, value: datetime = datetime(2026, 9, 10, 20, 31, tzinfo=UTC)) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class Sender:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[dict] = []
        self.failures = failures

    async def send_reminder(self, **kwargs) -> int:
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("secret Telegram response must not be stored")
        return len(self.calls)


def _environment(tmp_path: Path, *, status: str = "open", close: str = "2026-09-10T21:00:00Z"):
    path = tmp_path / "warehouse.db"
    UserDatabase(path).initialize()
    repo = WarehouseReminderDatabase(path)
    with sqlite3.connect(path) as connection:
        validate_warehouse_reminder_schema(connection)
        source = connection.execute("SELECT id FROM warehouse_locations WHERE stable_id='bar'").fetchone()[0]
        connection.execute(
            """INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot, cutoff_snapshot,
                status, started_at_utc, scheduled_close_at_utc, revision,
                created_at_utc, updated_at_utc
            ) VALUES (?, '2026-09-10', 'Europe/Kyiv', '23:00', ?,
                      '2026-09-09T20:00:00Z', ?, 1, 'now', 'now')""",
            (source, status, close),
        )
        shift_id = connection.execute("SELECT id FROM warehouse_shifts").fetchone()[0]
        connection.commit()
    settings = WarehouseReminderSettingsService(repo, AccessService(frozenset({1})), Clock())
    return path, repo, settings, source, shift_id


def _enable(settings, source: int, revision: int = 0) -> dict:
    return settings.save(source, {
        "enabled": 1, "target_chat_id": -100, "target_title": "Бар",
        "timezone": "Europe/Kyiv", "first": 30, "repeat": 30, "maximum": 3,
    }, revision, 1)


def _event_rows(path: Path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT sequence_number, scheduled_at_utc, status, attempt_count, last_error_code FROM warehouse_reminder_events ORDER BY sequence_number").fetchall()


def test_defaults_permissions_revision_and_schedule(tmp_path: Path) -> None:
    path, repo, settings, source, _ = _environment(tmp_path)
    assert settings.get(source, 1)["enabled"] == 0
    with pytest.raises(AccessDeniedError):
        settings.get(source, 2)
    saved = _enable(settings, source)
    assert saved["revision"] == 1
    with pytest.raises(WarehouseReminderConflict):
        _enable(settings, source, revision=0)
    with pytest.raises(ValueError, match="chat ID"):
        settings.update(source, {"target_chat_id": "@username"}, 1, 1)
    service = WarehouseReminderService(repo, Sender(), Clock())
    shift = {"scheduled_close_at_utc": "2026-09-10T21:00:00Z"}
    assert service.schedules(shift, saved) == [
        (1, "2026-09-10T20:30:00Z"),
        (2, "2026-09-10T21:00:00Z"),
        (3, "2026-09-10T21:30:00Z"),
    ]
    assert all(len(button.callback_data.encode()) <= 64 for row in reminder_settings_keyboard(saved).inline_keyboard for button in row)
    assert path.exists()


def test_kyiv_summer_and_winter_dst_use_shift_utc_snapshot(tmp_path: Path) -> None:
    _, repo, settings, source, _ = _environment(tmp_path)
    saved = _enable(settings, source)
    service = WarehouseReminderService(repo, Sender(), Clock())
    # 23:00 Kyiv is 20:00Z in summer (UTC+3) and 21:00Z in winter (UTC+2).
    assert service.schedules({"scheduled_close_at_utc": "2026-07-01T20:00:00Z"}, saved)[0] == (1, "2026-07-01T19:30:00Z")
    assert service.schedules({"scheduled_close_at_utc": "2026-01-01T21:00:00Z"}, saved)[0] == (1, "2026-01-01T20:30:00Z")
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(service.run_once(datetime(2026, 9, 10, 20, 31)))


def test_disabled_no_send_open_shift_and_callbacks_are_existing_flows(tmp_path: Path) -> None:
    _, repo, _, _, _ = _environment(tmp_path)
    sender = Sender()
    asyncio.run(WarehouseReminderService(repo, sender, Clock()).run_once())
    assert sender.calls == []


def test_open_shift_sends_once_and_restart_does_not_duplicate_sent_event(tmp_path: Path) -> None:
    path, repo, settings, source, _ = _environment(tmp_path)
    _enable(settings, source)
    first = Sender()
    asyncio.run(WarehouseReminderService(repo, first, Clock()).run_once())
    second = Sender()
    asyncio.run(WarehouseReminderService(WarehouseReminderDatabase(path), second, Clock()).run_once())
    assert len(first.calls) == 1
    assert second.calls == []
    assert [button.text for row in first.calls[0]["reply_markup"].inline_keyboard for button in row] == ["✅ Закрыть смену", "📋 Записи за смену"]
    assert _event_rows(path) == [
        (1, "2026-09-10T20:30:00Z", "sent", 1, None),
        (2, "2026-09-10T21:00:00Z", "pending", 0, None),
        (3, "2026-09-10T21:30:00Z", "pending", 0, None),
    ]


@pytest.mark.parametrize("status", ["closing", "closed"])
def test_closing_or_closed_shift_cancels_due_events_without_sending(tmp_path: Path, status: str) -> None:
    path, repo, settings, source, shift_id = _environment(tmp_path)
    _enable(settings, source)
    repo.ensure_events(shift_id, [(1, "2026-09-10T20:30:00Z")], "2026-09-10T20:00:00Z")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE warehouse_shifts SET status=? WHERE id=?", (status, shift_id))
        connection.commit()
    sender = Sender()
    asyncio.run(WarehouseReminderService(repo, sender, Clock()).run_once())
    assert sender.calls == []
    assert _event_rows(path)[0][2] == "cancelled"


def test_failure_is_retryable_and_raw_telegram_error_is_not_persisted(tmp_path: Path) -> None:
    path, repo, settings, source, _ = _environment(tmp_path)
    _enable(settings, source)
    sender = Sender(failures=1)
    service = WarehouseReminderService(repo, sender, Clock())
    asyncio.run(service.run_once())
    asyncio.run(service.run_once())
    assert len(sender.calls) == 2
    assert _event_rows(path)[0] == (1, "2026-09-10T20:30:00Z", "sent", 2, None)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT last_error_text FROM warehouse_reminder_events").fetchone()[0] is None


def test_stale_sending_is_recovered_then_retried(tmp_path: Path) -> None:
    path, repo, settings, source, shift_id = _environment(tmp_path)
    _enable(settings, source)
    repo.ensure_events(shift_id, [(1, "2026-09-10T20:30:00Z")], "2026-09-10T19:00:00Z")
    assert repo.claim(1, "2026-09-10T20:00:00Z") is not None
    sender = Sender()
    asyncio.run(WarehouseReminderService(repo, sender, Clock()).run_once())
    assert len(sender.calls) == 1
    assert _event_rows(path)[0] == (1, "2026-09-10T20:30:00Z", "sent", 2, None)


def test_concurrent_ticks_claim_only_once_and_network_has_no_sqlite_lock(tmp_path: Path) -> None:
    path, repo, settings, source, _ = _environment(tmp_path)
    _enable(settings, source)

    class BlockingSender(Sender):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_reminder(self, **kwargs) -> int:
            self.calls.append(kwargs)
            # This would fail with "database is locked" if the claim transaction stayed open.
            with sqlite3.connect(path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            self.entered.set()
            await self.release.wait()
            return 7

    async def scenario() -> None:
        sender = BlockingSender()
        one = WarehouseReminderService(repo, sender, Clock())
        two = WarehouseReminderService(WarehouseReminderDatabase(path), sender, Clock())
        first = asyncio.create_task(one.run_once())
        await sender.entered.wait()
        await two.run_once()
        sender.release.set()
        await first
        assert len(sender.calls) == 1

    asyncio.run(scenario())
    assert _event_rows(path)[0] == (1, "2026-09-10T20:30:00Z", "sent", 1, None)


def test_worker_is_cancellable_without_real_sleep(tmp_path: Path) -> None:
    _, repo, _, _, _ = _environment(tmp_path)

    async def scenario() -> None:
        service = WarehouseReminderService(repo, Sender(), Clock())
        entered = asyncio.Event()

        async def run_once() -> None:
            entered.set()

        service.run_once = run_once  # type: ignore[method-assign]
        task = asyncio.create_task(service.run_forever(interval=60))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_worker_starts_only_with_service_and_stops_cleanly() -> None:
    class Worker:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def run_forever(self) -> None:
            self.started.set()
            await asyncio.Event().wait()

    async def scenario() -> None:
        assert start_reminder_worker(None) is None
        worker = Worker()
        task = start_reminder_worker(worker)  # type: ignore[arg-type]
        assert task is not None
        await worker.started.wait()
        await stop_reminder_worker(task)
        assert task.cancelled()

    asyncio.run(scenario())
