import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.seed_service import build_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseOperationsError,
    WarehouseRevisionConflict,
)
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture(scope="module")
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse-operations") / "template.db"
    UserDatabase(path).initialize()
    seed = build_warehouse_seed(ROOT / "source" / "warehouse_product_candidates.json")
    WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    return path


@pytest.fixture
def environment(database_template: Path, tmp_path: Path):
    path = tmp_path / "warehouse.db"
    shutil.copyfile(database_template, path)
    users = UserService(UserDatabase(path))
    for user_id in (10, 20, 30, 40, 50):
        users.ensure_user(user_id)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    users.set_role(30, Role.ACCOUNTANT, actor_user_id=10)
    users.set_role(40, Role.USER, actor_user_id=10)
    users.set_role(50, Role.BLOCKED, actor_user_id=10)
    access = AuthService(frozenset({1}), users)
    repository = WarehouseOperationsDatabase(path)
    clock = FrozenClock()
    service = WarehouseOperationsService(repository, access, clock=clock)
    return path, users, repository, service, clock


def _prepare(service, text="списание: апельсин 0.15", *, actor=20, message=1):
    return service.prepare_batch(
        actor_user_id=actor, source_chat_id=777,
        source_message_id=message, raw_text=text,
    )


def test_prepare_ready_batch_shift_snapshot_and_idempotency(environment) -> None:
    _, _, _, service, clock = environment
    first = _prepare(service)
    second = _prepare(service)
    assert first["id"] == second["id"]
    assert first["status"] == "ready"
    assert first["lines"][0]["quantity_scaled"] == 150_000
    assert first["lines"][0]["input_unit_text"] is None
    assert first["shift_id"] == second["shift_id"]
    clock.value = datetime(2026, 9, 8, 21, 1, tzinfo=UTC)
    assert service.repository.get_batch(first["id"])["shift_id"] == first["shift_id"]


def test_unresolved_persists_and_can_be_corrected_or_cancelled(environment) -> None:
    _, _, _, service, _ = environment
    batch = _prepare(service, "списание: совершенно неизвестное 1", message=2)
    assert batch["status"] == "needs_resolution"
    assert batch["lines"][0]["error_code"] in {"unknown_product", "ambiguous_product"}
    fixed = service.replace_line_input(
        batch["id"], batch["lines"][0]["id"], "апельсин 150 г",
        actor_user_id=20,
    )
    assert fixed["status"] == "ready"
    cancelled = service.cancel_batch(fixed["id"], actor_user_id=20)
    assert cancelled["status"] == "cancelled"
    assert cancelled["lines"][0]["status"] == "cancelled"


def test_fuzzy_candidate_requires_explicit_product_resolution(environment) -> None:
    _, _, repository, service, _ = environment
    batch = _prepare(service, "списание: апелсин 1", message=22)
    line = batch["lines"][0]
    assert line["status"] == "ambiguous"
    assert line["product_id"] is None
    candidate_id = line["candidates"][0]["product_id"]
    resolved = service.resolve_product(
        batch["id"], line["id"], candidate_id, actor_user_id=20
    )
    assert resolved["status"] == "ready"
    assert resolved["lines"][0]["product_id"] == candidate_id

    lime = next(item for item in repository.products() if item.normalized_name == "лайм")
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE warehouse_products SET is_active=0 WHERE id=?", (lime.id,))
        connection.commit()
    inactive = _prepare(service, "списание: лайм 1", message=23)
    assert inactive["lines"][0]["product_id"] is None
    assert lime.id not in {
        item["product_id"] for item in inactive["lines"][0]["candidates"]
    }


def test_commit_is_atomic_creates_snapshots_and_audit(environment, monkeypatch) -> None:
    path, _, repository, service, _ = environment
    batch = _prepare(
        service, "списание:\nапельсин 0.15\nлайм 100 г (испорчен)", message=3
    )
    operations = service.commit_batch(batch["id"], actor_user_id=20)
    assert len(operations) == 2
    assert operations[0]["product_name_snapshot"] == "Апельсин"
    assert operations[1]["reason_text"] == "испорчен"
    with sqlite3.connect(path) as connection:
        events = connection.execute(
            "SELECT event_type FROM warehouse_audit_events ORDER BY id"
        ).fetchall()
    assert [row[0] for row in events].count("operation_created") == 2
    assert "batch_committed" in {row[0] for row in events}
    with pytest.raises(WarehouseOperationsError, match="not ready"):
        service.commit_batch(batch["id"], actor_user_id=20)

    other = _prepare(service, "списание:\nапельсин 1\nлайм 1", message=4)
    original_audit = repository._audit
    calls = 0

    def fail_last(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("audit failed")
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(repository, "_audit", fail_last)
    before = len(repository.list_operations(other["shift_id"]))
    with pytest.raises(RuntimeError, match="audit failed"):
        service.commit_batch(other["id"], actor_user_id=20)
    assert len(repository.list_operations(other["shift_id"])) == before
    assert repository.get_batch(other["id"])["status"] == "ready"


def test_commit_revalidates_product_and_rolls_back_all_lines(environment) -> None:
    path, _, repository, service, _ = environment
    batch = _prepare(service, "списание:\nапельсин 1\nлайм 1", message=5)
    product_id = batch["lines"][-1]["product_id"]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE warehouse_products SET is_active=0 WHERE id=?", (product_id,)
        )
        connection.commit()
    with pytest.raises(WarehouseOperationsError, match="Product changed"):
        service.commit_batch(batch["id"], actor_user_id=20)
    assert repository.list_operations(batch["shift_id"]) == []


def test_transfer_destination_and_operation_edit_delete(environment) -> None:
    path, _, _, service, _ = environment
    batch = _prepare(
        service, "перемещение: сахар 1 кг -> кухня (запас)", message=6
    )
    assert batch["status"] == "ready"
    operation = service.commit_batch(batch["id"], actor_user_id=10)[0]
    assert operation["destination_location_id"] is not None
    changed = service.update_quantity(
        operation["id"], "500", "г", expected_revision=1, actor_user_id=20
    )
    assert changed["quantity_scaled"] == 500_000
    products = service.repository.products()
    replacement = next(
        item for item in products
        if item.base_unit.value == "kg" and item.id != operation["product_id"]
    )
    changed = service.update_product(
        operation["id"], replacement.id, expected_revision=2, actor_user_id=20
    )
    assert changed["product_name_snapshot"] == replacement.official_name
    confectionery = next(
        item for item in service.repository.locations()
        if item.normalized_name == "кондитерка"
    )
    changed = service.update_destination(
        operation["id"], confectionery.id, expected_revision=3, actor_user_id=20
    )
    assert changed["destination_location_id"] == confectionery.id
    noted = service.update_note(
        operation["id"], reason_text=None, comment_text="исправлено",
        expected_revision=4, actor_user_id=10,
    )
    assert noted["revision"] == 5
    with pytest.raises(WarehouseRevisionConflict):
        service.update_note(
            operation["id"], reason_text=None, comment_text=None,
            expected_revision=1, actor_user_id=20,
        )
    deleted = service.delete_operation(
        operation["id"], expected_revision=5, actor_user_id=20,
        deletion_reason="ошибка ввода",
    )
    assert deleted["is_deleted"] == 1
    with pytest.raises(WarehouseOperationsError, match="Deleted"):
        service.update_quantity(
            operation["id"], "1", "kg", expected_revision=6, actor_user_id=20
        )
    with sqlite3.connect(path) as connection:
        audit = connection.execute(
            "SELECT before_json, after_json FROM warehouse_audit_events "
            "WHERE event_type='operation_deleted'"
        ).fetchone()
    assert audit[0] and audit[1]


def test_manual_operation_uses_manual_origin(environment) -> None:
    _, _, repository, service, _ = environment
    product = next(item for item in repository.products() if item.normalized_name == "апельсин")
    operation = service.create_manual_operation(
        actor_user_id=20, operation_type="writeoff", product_id=product.id,
        quantity_text="150", unit_text="г", reason_text="ручной ввод",
    )
    assert operation["origin"] == "manual"
    assert operation["quantity_scaled"] == 150_000


def test_closed_shift_rejects_mutation(environment) -> None:
    path, _, _, service, _ = environment
    batch = _prepare(service, message=7)
    operation = service.commit_batch(batch["id"], actor_user_id=20)[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE warehouse_shifts SET status='closed', "
            "closed_at_utc='2026-09-08T21:00:00Z', closed_by=10 "
            "WHERE id=?", (operation["shift_id"],)
        )
        connection.commit()
    with pytest.raises(WarehouseOperationsError, match="not open"):
        service.update_quantity(
            operation["id"], "1", None, expected_revision=1, actor_user_id=20
        )


@pytest.mark.parametrize("actor", [1, 10, 20])
def test_mutating_roles_are_allowed(environment, actor) -> None:
    _, _, _, service, _ = environment
    assert _prepare(service, actor=actor, message=100 + actor)["status"] == "ready"


@pytest.mark.parametrize("actor", [30, 40, 50])
def test_read_and_mutation_permissions(environment, actor) -> None:
    _, _, _, service, _ = environment
    if actor == 30:
        assert service.list_operations(1, actor_user_id=actor) == []
    else:
        with pytest.raises(AccessDeniedError):
            service.list_operations(1, actor_user_id=actor)
    with pytest.raises(AccessDeniedError):
        _prepare(service, actor=actor, message=200 + actor)


def test_role_change_after_prepare_is_rechecked(environment) -> None:
    _, users, _, service, _ = environment
    batch = _prepare(service, message=8)
    users.set_role(20, Role.USER, actor_user_id=10)
    with pytest.raises(AccessDeniedError):
        service.commit_batch(batch["id"], actor_user_id=20)
