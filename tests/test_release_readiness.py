"""Phase 10A migration, seed, and offline end-to-end rehearsals."""

from __future__ import annotations

import asyncio
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.import_service import WarehouseSeedImportService
from app.services.warehouse.schedule_service import WarehouseScheduleService
from app.services.warehouse.seed_service import load_warehouse_seed
from app.storage import migrations
from app.storage.user_database import UserDatabase
from app.storage.warehouse_correction_database import WarehouseCorrectionDatabase
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase
from tests.test_warehouse_corrections import (  # noqa: F401
    ROOT,
    _finish_closing,
    _product,
    _recipient,
    correction_template,
    environment,
)


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_report(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 5000 + len(self.calls)


def _delivery(environment, sender: FakeSender) -> WarehouseReportDeliveryService:
    return WarehouseReportDeliveryService(
        WarehouseDeliveryDatabase(environment["path"]),
        environment["reports"], environment["access"], sender,
        clock=environment["clock"],
    )


def test_seed_rehearsal_dry_run_apply_repeat_and_expected_inventory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-v11.db"
    UserDatabase(database).initialize()
    source = ROOT / "source" / "warehouse_products_seed.json"
    seed = load_warehouse_seed(source)
    service = WarehouseSeedImportService(database)

    dry_run = service.run(source)
    applied = service.run(source, apply=True)
    repeated = service.run(source, apply=True)

    assert len(seed.products) == 414
    assert sum(product.is_active for product in seed.products) == 414
    assert sum(product.review_status == "review_required" for product in seed.products) == 18
    assert sum(product.review_status == "owner_required" for product in seed.products) == 2
    assert all(product.source_candidate_ids for product in seed.products)
    assert any(product.warnings for product in seed.products)
    assert len(dry_run.create) == len(applied.create) == 414
    assert applied.applied is True
    assert len(repeated.unchanged) == 414 and repeated.create == ()
    assert len(repeated.rule_unchanged) == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*),SUM(is_active) FROM warehouse_products"
        ).fetchone() == (414, 414)
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_product_aliases"
        ).fetchone()[0] == 8
        assert {row[0] for row in connection.execute(
            "SELECT name FROM warehouse_locations"
        )} == {"Бар", "Кухня", "Кондитерка"}
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_required_rules"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_rehearsal_fresh_v10_repeat_integrity_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    UserDatabase(fresh).initialize()
    UserDatabase(fresh).initialize()
    with sqlite3.connect(fresh) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    upgraded = tmp_path / "v10.db"
    with sqlite3.connect(upgraded) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v10(connection)
        connection.execute(
            "INSERT INTO users(telegram_user_id,username,first_name,last_name,role,"
            "is_active,first_seen_at,last_seen_at,created_by,updated_at,has_started) "
            "VALUES(10,'admin','Admin',NULL,'admin',1,'before','before',1,'before',1)"
        )
        connection.commit()
    UserDatabase(upgraded).initialize()
    UserDatabase(upgraded).initialize()
    with sqlite3.connect(upgraded) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute(
            "SELECT username,role,has_started FROM users WHERE telegram_user_id=10"
        ).fetchone() == ("admin", "admin", 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    rollback = tmp_path / "rollback-v10.db"
    with sqlite3.connect(rollback) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v10(connection)
        connection.commit()

    def fail(connection):
        connection.execute("CREATE TABLE phase10_partial(id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(migrations, "create_warehouse_correction_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError):
        UserDatabase(rollback).initialize()
    with sqlite3.connect(rollback) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='phase10_partial'"
        ).fetchone() is None


def test_offline_end_to_end_release_readiness(environment) -> None:
    operations = environment["operations"]
    orange = _product(environment, "Апельсин")
    sugar = _product(environment, "Сахар")
    destination = operations.destination_options(actor_user_id=20)[0]
    writeoff = operations.create_manual_operation(
        actor_user_id=20, operation_type="writeoff", product_id=orange.id,
        quantity_text="0.15", reason_text="offline smoke",
    )
    edited = operations.update_quantity(
        writeoff["id"], "0.2", None,
        expected_revision=writeoff["revision"], actor_user_id=20,
    )
    operations.create_manual_operation(
        actor_user_id=20, operation_type="transfer", product_id=sugar.id,
        quantity_text="0.25", destination_location_id=destination.id,
        comment_text="offline smoke",
    )
    batch = operations.prepare_batch(
        actor_user_id=20, source_chat_id=777, source_message_id=919,
        raw_text="проработка: Offline smoke\nапельсин 0.1 кг",
    )
    operations.commit_batch(batch["id"], actor_user_id=20)
    assert edited["quantity_scaled"] == 200_000

    shift_id = _finish_closing(environment)
    original = environment["reports"].build_final(
        shift_id, actor_user_id=20
    )["report"]
    original_path = environment["project"] / original["relative_file_path"]
    with zipfile.ZipFile(original_path) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    assert all(name in workbook for name in ("Списание", "Перемещение", "Проработка"))

    first = _recipient(environment["path"], -10101, "Offline recipient")
    sender = FakeSender()
    delivery = _delivery(environment, sender)
    sent = asyncio.run(delivery.deliver(
        original["id"], actor_user_id=20, recipient_ids=(first,)
    ))
    assert sent["delivered"] == 1 and sent["failed"] == 0

    schedules = WarehouseScheduleDatabase(environment["path"])
    source_id = operations.repository.runtime_settings()["source_location_id"]
    daily = schedules.create(
        {"name": "Offline daily", "schedule_type": "daily",
         "source_location_id": source_id, "timezone": "Europe/Kyiv",
         "delay": 15, "max_wait": 1440, "is_active": 1},
        (first,), 10, "2026-09-10T19:00:00Z",
    )
    once = schedules.create(
        {"name": "Offline once", "schedule_type": "once",
         "report_id": original["id"], "timezone": "Europe/Kyiv",
         "run_at_utc": "2026-09-11T10:00:00Z", "max_wait": 1440,
         "is_active": 1},
        (first,), 10, "2026-09-10T19:00:00Z",
    )
    schedules.create_daily_jobs("2026-09-10T21:30:00Z")
    daily_job = schedules.jobs(daily["id"])[0]
    once_job = schedules.jobs(once["id"])[0]
    assert schedules.report_for_job(daily_job)["id"] == original["id"]
    assert schedules.report_for_job(once_job)["id"] == original["id"]

    shift = operations.repository.shift(shift_id)
    correction = environment["corrections"].open_correction(
        shift_id, "Offline correction",
        expected_shift_revision=shift["revision"], actor_user_id=10,
    )
    current_operation = operations.repository.get_operation(writeoff["id"])
    environment["corrections"].update_quantity(
        correction["id"], writeoff["id"], "0.3", None,
        expected_revision=current_operation["revision"], actor_user_id=10,
    )
    current = environment["corrections"].session(
        correction["id"], actor_user_id=10
    )
    released = environment["corrections"].release_correction(
        correction["id"], expected_revision=current["revision"], actor_user_id=10
    )
    assert released["report"]["version"] == 2
    corrected = asyncio.run(environment["corrections"].deliver_correction(
        correction["id"], delivery, actor_user_id=10,
        recipient_ids=(first,),
    ))
    assert corrected["delivered"] == 1
    assert schedules.report_for_job(daily_job)["id"] == original["id"]
    with pytest.raises(ValueError, match="explicit manual"):
        asyncio.run(delivery.deliver_scheduled(
            released["report"]["id"], recipient_ids=(first,)
        ))

    accountant = environment["corrections"].result(
        correction["id"], actor_user_id=30
    )
    assert accountant["can_manage"] is False
    with pytest.raises(AccessDeniedError):
        environment["corrections"].delivery_preview(
            correction["id"], actor_user_id=30
        )
    recovered = environment["corrections"].recover_correction(
        correction["id"], actor_user_id=10
    )
    assert recovered["report"]["id"] == released["report"]["id"]
    assert len(sender.calls) == 2
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_report_deliveries"
        ).fetchone()[0] == 2
        events = {row[0] for row in connection.execute(
            "SELECT event_type FROM warehouse_audit_events"
        )}
        assert "warehouse_report_ready" in events
        assert "correction_completed" in events
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert original_path.is_relative_to(environment["project"])
    assert Path(released["artifact"].path).is_relative_to(environment["project"])
