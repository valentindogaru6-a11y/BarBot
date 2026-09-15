"""Required-writeoff fulfillment and safe shift-closing workflows."""

import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.models.warehouse.closing import (
    RequiredQuantityMode,
    RequiredRuleInput,
)
from app.models.warehouse.nomenclature import BaseUnit
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.quantity_service import QuantityError
from app.services.warehouse.seed_service import load_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_database import (
    WarehouseClosingConflict,
    WarehouseClosingDatabase,
    WarehouseClosingError,
)
from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseOperationsError,
)
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture(scope="module")
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse-closing") / "template.db"
    UserDatabase(path).initialize()
    seed = load_warehouse_seed(ROOT / "source" / "warehouse_products_seed.json")
    result = WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    assert len(result.rule_create) == 2
    return path


@pytest.fixture
def environment(database_template: Path, tmp_path: Path):
    path = tmp_path / "warehouse.db"
    shutil.copyfile(database_template, path)
    users = UserService(UserDatabase(path))
    for user_id in (10, 20, 30, 40):
        users.ensure_user(user_id)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    users.set_role(30, Role.ACCOUNTANT, actor_user_id=10)
    access = AuthService(frozenset({1}), users)
    clock = FrozenClock()
    operations = WarehouseOperationsService(
        WarehouseOperationsDatabase(path), access, clock=clock
    )
    closing = WarehouseClosingService(
        WarehouseClosingDatabase(path), operations, access, clock=clock
    )
    return path, users, operations, closing, clock


def _product(operations, name: str):
    return next(
        product for product in operations.repository.products()
        if product.official_name == name
    )


def _requirement(session: dict, name: str) -> dict:
    return next(
        row for row in session["requirements"] if row["product_name"] == name
    )


def test_snapshot_order_daily_rules_and_global_edit_isolation(environment) -> None:
    _, _, _, closing, _ = environment
    session = closing.start(actor_user_id=20)
    assert [row["product_name"] for row in session["requirements"]] == [
        "Молотый кофе из гриндера",
        "Остаток молока в питчерах",
    ]
    coffee = session["requirements"][0]
    rule = closing.get_rule(int(coffee["rule_id"]), actor_user_id=10)
    assert rule is not None
    closing.patch_rule(
        int(rule["id"]),
        {"question_text": "Новый вопрос только для следующей смены"},
        expected_revision=int(rule["revision"]),
        actor_user_id=10,
    )
    refreshed = closing.resume(int(session["id"]), actor_user_id=20)
    assert refreshed["requirements"][0]["question_text"] == (
        "Сколько молотого кофе осталось в гриндере?"
    )


def test_existing_exact_operation_satisfies_and_edit_delete_reopen(environment) -> None:
    _, _, operations, closing, _ = environment
    coffee = _product(operations, "Молотый кофе из гриндера")
    other = next(
        product for product in operations.repository.products()
        if product.base_unit is BaseUnit.KG and product.id != coffee.id
    )
    operation = operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=coffee.id,
        quantity_text="0.05",
    )
    session = closing.start(actor_user_id=20)
    assert _requirement(session, coffee.official_name)["status"] == "satisfied"

    changed = operations.update_product(
        int(operation["id"]),
        other.id,
        expected_revision=int(operation["revision"]),
        actor_user_id=20,
    )
    session = closing.resume(int(session["id"]), actor_user_id=20)
    assert _requirement(session, coffee.official_name)["status"] == "pending"

    exact = operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=coffee.id,
        quantity_text="0.04",
    )
    session = closing.resume(int(session["id"]), actor_user_id=20)
    assert _requirement(session, coffee.official_name)["status"] == "satisfied"
    operations.delete_operation(
        int(exact["id"]),
        expected_revision=int(exact["revision"]),
        actor_user_id=20,
        deletion_reason="исправление теста",
    )
    session = closing.resume(int(session["id"]), actor_user_id=20)
    assert _requirement(session, coffee.official_name)["status"] == "pending"
    assert changed["product_id"] == other.id


def test_wrong_type_does_not_satisfy_requirement(environment) -> None:
    _, _, operations, closing, _ = environment
    coffee = _product(operations, "Молотый кофе из гриндера")
    destination = operations.destination_options(actor_user_id=20)[0]
    operations.create_manual_operation(
        actor_user_id=20,
        operation_type="transfer",
        product_id=coffee.id,
        quantity_text="0.1",
        destination_location_id=destination.id,
    )
    batch = operations.prepare_batch(
        actor_user_id=20,
        source_chat_id=777,
        source_message_id=157,
        raw_text=(
            "проработка: Тест кофе\n"
            f"{coffee.official_name} 0.05 кг"
        ),
    )
    operations.commit_batch(int(batch["id"]), actor_user_id=20)
    session = closing.start(actor_user_id=20)
    assert _requirement(session, coffee.official_name)["status"] == "pending"


def test_recommended_and_manual_quantities_then_atomic_finalize(environment) -> None:
    path, _, operations, closing, _ = environment
    session = closing.start(actor_user_id=20)
    coffee = _requirement(session, "Молотый кофе из гриндера")
    session = closing.recommended(
        int(session["id"]), int(coffee["id"]), actor_user_id=20
    )
    milk = _requirement(session, "Остаток молока в питчерах")
    decimal_preview = closing.quantity_preview(
        int(session["id"]), int(milk["id"]), "0.35", actor_user_id=20
    )
    milliliter_preview = closing.quantity_preview(
        int(session["id"]), int(milk["id"]), "350 мл", actor_user_id=20
    )
    assert decimal_preview["quantity_scaled"] == 350_000
    assert milliliter_preview["quantity_scaled"] == 350_000
    with pytest.raises(QuantityError, match="несовместима"):
        closing.quantity_preview(
            int(session["id"]), int(milk["id"]), "1 кг", actor_user_id=20
        )
    ready = closing.save_quantity(milliliter_preview, actor_user_id=20)
    assert ready["status"] == "ready"
    result = closing.finalize(
        int(ready["id"]),
        expected_revision=int(ready["revision"]),
        actor_user_id=20,
    )
    assert result["shift_status"] == "closing"
    duplicate = closing.repository.finalize(
        int(ready["id"]),
        expected_revision=1,
        actor_user_id=20,
        now="2026-09-10T20:01:00Z",
    )
    assert duplicate["shift"]["status"] == "closing"
    repeated = closing.start(actor_user_id=20)
    assert repeated["id"] == ready["id"]
    with pytest.raises(WarehouseOperationsError, match="not open"):
        operations.create_manual_operation(
            actor_user_id=20,
            operation_type="writeoff",
            product_id=_product(
                operations, "Молотый кофе из гриндера"
            ).id,
            quantity_text="0.01",
        )
    with sqlite3.connect(path) as connection:
        origins = dict(
            connection.execute(
                "SELECT origin, COUNT(*) FROM warehouse_operations GROUP BY origin"
            ).fetchall()
        )
        assert origins == {"required_rule": 2}
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_audit_events "
            "WHERE event_type='shift_prepared_for_report'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_closing_sessions"
        ).fetchone()[0] == 1


def test_not_applicable_reason_and_false_already_written_off(environment) -> None:
    path, _, _, closing, _ = environment
    session = closing.start(actor_user_id=20)
    coffee = _requirement(session, "Молотый кофе из гриндера")
    with pytest.raises(WarehouseClosingError, match="No matching"):
        closing.mark_not_applicable(
            int(session["id"]),
            int(coffee["id"]),
            "Уже списано ранее",
            actor_user_id=20,
        )
    session = closing.mark_not_applicable(
        int(session["id"]),
        int(coffee["id"]),
        "Позиция не использовалась",
        actor_user_id=20,
    )
    resolved = _requirement(session, "Молотый кофе из гриндера")
    assert resolved["status"] == "not_applicable"
    assert resolved["resolution_reason"] == "Позиция не использовалась"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_operations"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT reason_text FROM warehouse_audit_events "
            "WHERE event_type='requirement_not_applicable'"
        ).fetchone()[0] == "Позиция не использовалась"


def test_cancel_reopens_shift_and_next_session_reuses_operations(environment) -> None:
    _, _, _, closing, _ = environment
    first = closing.start(actor_user_id=20)
    coffee = first["requirements"][0]
    first = closing.recommended(
        int(first["id"]), int(coffee["id"]), actor_user_id=20
    )
    cancelled = closing.cancel(int(first["id"]), actor_user_id=20)
    assert cancelled["status"] == "cancelled"
    assert cancelled["shift_status"] == "open"
    second = closing.start(actor_user_id=20, trigger_type="recovery")
    assert second["id"] != first["id"]
    assert second["requirements"][0]["status"] == "satisfied"
    assert second["current_requirement_id"] == second["requirements"][1]["id"]


def test_two_closers_share_session_and_role_is_rechecked(environment) -> None:
    _, users, _, closing, _ = environment
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda actor: closing.start(actor_user_id=actor), (10, 20)))
    assert sessions[0]["id"] == sessions[1]["id"]
    users.set_role(20, Role.USER, actor_user_id=10)
    with pytest.raises(AccessDeniedError):
        closing.recommended(
            int(sessions[0]["id"]),
            int(sessions[0]["requirements"][0]["id"]),
            actor_user_id=20,
        )


def test_accountant_status_is_read_only(environment) -> None:
    _, _, _, closing, _ = environment
    closing.start(actor_user_id=20)
    overview = closing.overview(actor_user_id=30)
    assert overview["requirements_total"] == 2
    assert closing.active_session(actor_user_id=30) is not None
    with pytest.raises(AccessDeniedError):
        closing.start(actor_user_id=30)


def test_admin_rule_crud_revision_and_weekday_applicability(environment) -> None:
    _, _, operations, closing, _ = environment
    product = next(
        item for item in operations.repository.products()
        if item.base_unit is BaseUnit.PCS
    )
    command = RequiredRuleInput(
        product_id=product.id,
        quantity_mode=RequiredQuantityMode.MANUAL,
        base_unit=BaseUnit.PCS,
        weekdays_mask=16,
        question_text="Сколько & штук осталось?",
        sort_order=5,
        allow_not_applicable=False,
    )
    created = closing.create_rule(command, actor_user_id=10)
    assert created["is_active"] == 1
    settings = operations.repository.runtime_settings()
    thursday = closing.repository.ensure_shift(
        {
            "source_location_id": int(settings["source_location_id"]),
            "business_date": "2026-09-10",
            "timezone": str(settings["timezone"]),
            "cutoff": str(settings["cutoff_local_time"]),
            "started_at_utc": "2026-09-10T00:00:00Z",
            "scheduled_close_at_utc": "2026-09-10T21:00:00Z",
            "now": "2026-09-10T00:00:00Z",
        }
    )
    friday = closing.repository.ensure_shift(
        {
            "source_location_id": int(settings["source_location_id"]),
            "business_date": "2026-09-11",
            "timezone": str(settings["timezone"]),
            "cutoff": str(settings["cutoff_local_time"]),
            "started_at_utc": "2026-09-11T00:00:00Z",
            "scheduled_close_at_utc": "2026-09-11T21:00:00Z",
            "now": "2026-09-11T00:00:00Z",
        }
    )
    thursday_names = {
        row["product_name"] for row in closing.repository.prepare_requirements(
            int(thursday["id"]), now="2026-09-10T00:01:00Z"
        )
    }
    friday_names = {
        row["product_name"] for row in closing.repository.prepare_requirements(
            int(friday["id"]), now="2026-09-11T00:01:00Z"
        )
    }
    assert product.official_name not in thursday_names
    assert product.official_name in friday_names
    with pytest.raises(AccessDeniedError):
        closing.list_rules(actor_user_id=20)
    updated = closing.patch_rule(
        int(created["id"]),
        {"is_active": False, "sort_order": 99},
        expected_revision=int(created["revision"]),
        actor_user_id=10,
    )
    assert updated["is_active"] == 0 and updated["revision"] == 2
    with pytest.raises(WarehouseClosingConflict):
        closing.patch_rule(
            int(created["id"]),
            {"sort_order": 2},
            expected_revision=1,
            actor_user_id=10,
        )


def test_failed_final_audit_rolls_back_shift_and_session(environment, monkeypatch) -> None:
    _, _, _, closing, _ = environment
    session = closing.start(actor_user_id=20)
    for requirement in session["requirements"]:
        session = closing.mark_not_applicable(
            int(session["id"]),
            int(requirement["id"]),
            "Остатков не было",
            actor_user_id=20,
        )
    assert session["status"] == "ready"

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(closing.repository, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit failed"):
        closing.finalize(
            int(session["id"]),
            expected_revision=int(session["revision"]),
            actor_user_id=20,
        )
    stored = closing.repository.session(int(session["id"]))
    assert stored is not None
    assert stored["status"] == "ready"
    assert stored["shift_status"] == "waiting_required"
