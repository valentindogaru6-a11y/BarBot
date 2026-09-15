import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.seed_service import build_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseRevisionConflict,
)
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 8, 20, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("development") / "template.db"
    UserDatabase(path).initialize()
    seed = build_warehouse_seed(
        ROOT / "source" / "warehouse_product_candidates.json"
    )
    WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    return path


@pytest.fixture
def environment(template: Path, tmp_path: Path):
    path = tmp_path / "warehouse.db"
    shutil.copyfile(template, path)
    users = UserService(UserDatabase(path))
    for user_id in (10, 20, 30):
        users.ensure_user(user_id)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    users.set_role(30, Role.ACCOUNTANT, actor_user_id=10)
    access = AuthService(frozenset({1}), users)
    repository = WarehouseOperationsDatabase(path)
    service = WarehouseOperationsService(repository, access, clock=FrozenClock())
    return path, service


def test_atomic_development_commit_snapshot_group_and_audit(environment) -> None:
    path, service = environment
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=1,
        raw_text=(
            "проработка: Новый чай\n"
            "апельсин 0.2 кг\n"
            "лайм 0.1 кг (кислотность)"
        ),
    )
    assert batch["status"] == "ready"
    assert batch["development_title"] == "Новый чай"

    operations = service.commit_batch(batch["id"], actor_user_id=20)
    assert len(operations) == 2
    assert {row["operation_type"] for row in operations} == {"development"}
    assert {row["development_title"] for row in operations} == {"Новый чай"}
    assert {row["development_group_id"] for row in operations} == {batch["id"]}
    assert {row["destination_location_id"] for row in operations} == {None}
    with sqlite3.connect(path) as connection:
        events = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM warehouse_audit_events"
            )
        }
    assert "development_batch_created" in events
    assert "development_operation_created" in events
    assert "development_batch_committed" in events


def test_same_title_stays_in_separate_groups_and_rename_is_atomic(environment) -> None:
    _, service = environment
    first = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=2,
        raw_text="проработка: Одинаковая\nапельсин 0.1 кг\nлайм 0.1 кг",
    )
    first_operations = service.commit_batch(first["id"], actor_user_id=20)
    second = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=3,
        raw_text="проработка: Одинаковая\nапельсин 0.1 кг",
    )
    second_operations = service.commit_batch(second["id"], actor_user_id=20)
    assert first["id"] != second["id"]

    selected = first_operations[0]
    updated = service.rename_development(
        selected["id"],
        "Новое название",
        expected_revision=selected["revision"],
        actor_user_id=20,
    )
    assert updated["development_title"] == "Новое название"
    first_rows = service.list_operations(first_operations[0]["shift_id"], actor_user_id=20)
    assert {
        row["development_title"]
        for row in first_rows
        if row["development_group_id"] == first["id"]
    } == {"Новое название"}
    assert {
        row["development_title"]
        for row in first_rows
        if row["development_group_id"] == second["id"]
    } == {"Одинаковая"}
    with pytest.raises(WarehouseRevisionConflict):
        service.rename_development(
            selected["id"],
            "Устаревшее",
            expected_revision=selected["revision"],
            actor_user_id=20,
        )


def test_missing_title_is_persistent_and_promoted(environment) -> None:
    path, service = environment
    draft = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=4,
        raw_text="проработка:\nапельсин 0.2 кг",
    )
    assert draft["needs_development_title"] is True
    recovered = service.pending_intake(actor_user_id=20)
    assert recovered is not None and recovered["id"] == draft["id"]

    batch = service.complete_development_title(
        draft["id"], "Восстановленный проект", actor_user_id=20
    )
    assert batch["status"] == "ready"
    assert batch["development_title"] == "Восстановленный проект"
    with sqlite3.connect(path) as connection:
        status, batch_id = connection.execute(
            "SELECT status, batch_id FROM warehouse_development_title_drafts "
            "WHERE id=?",
            (draft["id"],),
        ).fetchone()
    assert (status, batch_id) == ("promoted", batch["id"])


def test_title_first_draft_recovers_before_ingredients(environment) -> None:
    path, service = environment
    draft = service.start_development_draft(
        "Название до restart",
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=44,
    )
    assert draft["status"] == "needs_ingredients"
    recovered = service.pending_intake(actor_user_id=20)
    assert recovered is not None
    assert recovered["needs_development_ingredients"] is True
    assert recovered["development_title"] == "Название до restart"

    batch = service.complete_development_ingredients(
        int(draft["id"]),
        "апельсин 0.2 кг\nлайм 0.1 кг",
        actor_user_id=20,
    )
    assert batch["status"] == "ready"
    assert batch["development_title"] == "Название до restart"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM warehouse_development_title_drafts WHERE id=?",
            (draft["id"],),
        ).fetchone()[0] == "promoted"


def test_development_soft_delete_and_accountant_read_only(environment) -> None:
    _, service = environment
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=5,
        raw_text="проработка: Удаление\nапельсин 0.1 кг",
    )
    operation = service.commit_batch(batch["id"], actor_user_id=20)[0]
    deleted = service.delete_operation(
        operation["id"],
        expected_revision=operation["revision"],
        actor_user_id=20,
        deletion_reason="тест",
    )
    assert deleted["is_deleted"] == 1
    assert service.list_operations(operation["shift_id"], actor_user_id=30) == []
    with pytest.raises(Exception):
        service.prepare_batch(
            actor_user_id=30,
            source_chat_id=777,
            source_message_id=6,
            raw_text="проработка: Запрещено\nапельсин 0.1 кг",
        )


def test_transfer_header_destination_is_applied_to_every_line(environment) -> None:
    _, service = environment
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=7,
        raw_text="перемещение на кухню:\nапельсин 0.1 кг\nлайм 0.2 кг",
    )
    assert batch["status"] == "ready"
    assert len({line["destination_location_id"] for line in batch["lines"]}) == 1
    service.cancel_batch(batch["id"], actor_user_id=20)

    conflict = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=8,
        raw_text="перемещение в кондитерку:\nапельсин 0.1 кг -> кухня",
    )
    assert conflict["status"] == "needs_resolution"
    assert conflict["lines"][0]["error_code"] == "header_destination_conflict"
