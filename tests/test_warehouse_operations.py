import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.user import Role
from app.models.warehouse import BaseUnit, ProductOperationType, WarehouseProductCreate
from app.handlers import warehouse as warehouse_handler
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.nomenclature_service import WarehouseNomenclatureService
from app.services.warehouse.seed_service import build_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseOperationsError,
    WarehouseRevisionConflict,
)
from app.storage.warehouse_database import WarehouseDatabase
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


def _prepare(service, text="списание: апельсин 0.15 кг", *, actor=20, message=1):
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
    assert first["lines"][0]["input_unit_text"] == "кг"
    assert first["shift_id"] == second["shift_id"]
    clock.value = datetime(2026, 9, 8, 21, 1, tzinfo=UTC)
    assert service.repository.get_batch(first["id"])["shift_id"] == first["shift_id"]


def test_unresolved_persists_and_can_be_corrected_or_cancelled(environment) -> None:
    _, _, _, service, _ = environment
    batch = _prepare(service, "списание: совершенно неизвестное 1 кг", message=2)
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


def test_syntax_error_persists_and_only_its_line_is_reparsed(environment) -> None:
    _, _, _, service, _ = environment
    batch = _prepare(
        service,
        "списание:\nапельсин много\nлайм 100 г",
        message=222,
    )
    assert batch["status"] == "needs_resolution"
    assert batch["lines"][0]["error_code"] == "invalid_quantity"
    assert batch["lines"][1]["status"] == "resolved"

    fixed = service.replace_line_input(
        batch["id"],
        batch["lines"][0]["id"],
        "апельсин 150 г",
        actor_user_id=20,
    )
    assert fixed["status"] == "ready"
    assert fixed["lines"][0]["quantity_scaled"] == 150_000
    assert fixed["lines"][1]["raw_line"] == "лайм 100 г"
    service.cancel_batch(batch["id"], actor_user_id=20)


def test_fuzzy_candidate_requires_explicit_product_resolution(environment) -> None:
    _, _, repository, service, _ = environment
    batch = _prepare(service, "списание: апелсин 1 кг", message=22)
    line = batch["lines"][0]
    assert line["status"] == "ambiguous"
    assert line["product_id"] is None
    candidate_id = line["candidates"][0]["product_id"]
    resolved = service.resolve_product(
        batch["id"], line["id"], candidate_id, actor_user_id=20
    )
    assert resolved["status"] == "ready"
    assert resolved["lines"][0]["product_id"] == candidate_id
    service.cancel_batch(batch["id"], actor_user_id=20)

    lime = next(item for item in repository.products() if item.normalized_name == "лайм")
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE warehouse_products SET is_active=0 WHERE id=?", (lime.id,))
        connection.commit()
    inactive = _prepare(service, "списание: лайм 1 кг", message=23)
    assert inactive["lines"][0]["product_id"] is None
    assert lime.id not in {
        item["product_id"] for item in inactive["lines"][0]["candidates"]
    }


def test_commit_is_atomic_creates_snapshots_and_audit(environment, monkeypatch) -> None:
    path, _, repository, service, _ = environment
    batch = _prepare(
        service, "списание:\nапельсин 0.15 кг\nлайм 100 г (испорчен)", message=3
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

    other = _prepare(service, "списание:\nапельсин 1 кг\nлайм 1 кг", message=4)
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
    batch = _prepare(service, "списание:\nапельсин 1 кг\nлайм 1 кг", message=5)
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
    operation = service.commit_batch(batch["id"], actor_user_id=20)[0]
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


def test_only_one_active_batch_and_batch_ownership(environment) -> None:
    _, _, _, service, _ = environment
    first = _prepare(service, "списание: апельсин 1", message=70)
    second = _prepare(service, "списание: лайм 1", message=71)
    assert second["id"] == first["id"]
    with pytest.raises(WarehouseOperationsError, match="another user"):
        service.resolve_product(
            first["id"], first["lines"][0]["id"],
            first["lines"][0]["product_id"], actor_user_id=10,
        )
    service.cancel_batch(first["id"], actor_user_id=20)
    assert service.owned_batch(first["id"], actor_user_id=20) is None


def test_aggregates_and_pages_exclude_soft_deleted_operations(environment) -> None:
    _, _, _, service, _ = environment
    first = _prepare(service, "списание: апельсин 0.15 кг", message=72)
    operation = service.commit_batch(first["id"], actor_user_id=20)[0]
    second = _prepare(service, "списание: апельсин 0.35 кг", message=73)
    service.commit_batch(second["id"], actor_user_id=20)
    totals = service.aggregates(operation["shift_id"], actor_user_id=30)
    orange = next(item for item in totals if item["product_name_snapshot"] == "Апельсин")
    assert orange["quantity_scaled"] == 500_000
    assert orange["operation_count"] == 2
    page, total = service.operation_page(
        operation["shift_id"], "writeoff", page=0, actor_user_id=30
    )
    assert len(page) == total == 2
    service.delete_operation(
        operation["id"], expected_revision=1, actor_user_id=20,
        deletion_reason="тест",
    )
    assert service.operation_page(
        operation["shift_id"], "writeoff", page=0, actor_user_id=30
    )[1] == 1
    totals = service.aggregates(operation["shift_id"], actor_user_id=30)
    assert next(item for item in totals if item["product_name_snapshot"] == "Апельсин")[
        "quantity_scaled"
    ] == 350_000


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


class _FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.state = value


class _FakeMessage:
    def __init__(self) -> None:
        self.answer = AsyncMock()


def _nomenclature(environment) -> WarehouseNomenclatureService:
    path, _, _, operations, _ = environment
    return WarehouseNomenclatureService(WarehouseDatabase(path), operations.access)


def _create_product(
    environment,
    name: str,
    *,
    base_unit: BaseUnit,
    operations: set[ProductOperationType],
):
    return _nomenclature(environment).create_product(
        10,
        WarehouseProductCreate(
            official_name=name,
            base_unit=base_unit,
            operation_types=operations,
        ),
    )


def test_missing_unit_is_not_unknown_product_and_requires_confirmation(
    environment, monkeypatch
) -> None:
    _, _, _, service, _ = environment
    product = _create_product(
        environment,
        "Кокосовое молоко",
        base_unit=BaseUnit.L,
        operations={ProductOperationType.TRANSFER},
    )
    batch = _prepare(
        service,
        "перемещение: кокосовое молоко 0.25 -> кухня",
        actor=10,
        message=901,
    )
    line = batch["lines"][0]
    assert line["product_text"] == "кокосовое молоко"
    assert line["input_quantity_text"] == "0.25"
    assert line["input_unit_text"] is None
    assert line["destination_text"] == "кухня"
    assert line["product_id"] == product.id
    assert line["error_code"] == "missing_unit"
    assert line["candidates"] == []

    state = _FakeState()
    message = _FakeMessage()
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    prompt = message.answer.await_args.args[0]
    assert prompt == (
        "Для “Кокосовое молоко” не указана единица измерения. "
        "Выберите единицу:"
    )

    resolved = service.resolve_unit(
        batch["id"], line["id"], "л", actor_user_id=10
    )
    resolved_line = resolved["lines"][0]
    assert resolved["status"] == "ready"
    assert resolved_line["quantity_scaled"] == 250_000
    assert Decimal(resolved_line["input_quantity_text"]) == Decimal("0.25")
    assert not isinstance(resolved_line["input_quantity_text"], float)


def test_unknown_product_uses_clean_product_text(environment, monkeypatch) -> None:
    _, _, _, service, _ = environment
    batch = _prepare(
        service,
        "перемещение: новый сироп 0.25 л -> кухня",
        actor=10,
        message=902,
    )
    line = batch["lines"][0]
    assert line["product_text"] == "новый сироп"
    assert line["input_quantity_text"] == "0.25"
    assert line["input_unit_text"] == "л"
    assert line["destination_text"] == "кухня"
    assert line["product_id"] is None
    assert line["error_code"] in {"unknown_product", "ambiguous_product"}

    state = _FakeState()
    message = _FakeMessage()
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    assert message.answer.await_args.args[0].startswith(
        "Продукт “новый сироп” не найден в номенклатуре."
    )
    markup = message.answer.await_args.kwargs["reply_markup"]
    texts = {button.text for row in markup.inline_keyboard for button in row}
    assert "➕ Добавить “новый сироп” в номенклатуру" in texts
    assert {"✏️ Ввести строку заново", "🗑 Убрать строку", "❌ Отменить всё"} <= texts


@pytest.mark.parametrize(
    "value",
    (
        "Новый сироп 0.25 л",
        "Новый сироп -> кухня",
        "Новый сироп (для кухни)",
    ),
)
def test_canonical_product_name_rejects_intake_fragments(value) -> None:
    with pytest.raises(ValueError):
        warehouse_handler._canonical_product_name(value)


@pytest.mark.parametrize("value", ("Новый сироп в кухню", "Новый сироп из бара"))
def test_canonical_product_name_rejects_locations(value) -> None:
    with pytest.raises(ValueError):
        warehouse_handler._canonical_product_name(value, ("Кухня", "Бар"))


def test_confirmed_unknown_product_creation_continues_and_is_idempotent(
    environment, monkeypatch
) -> None:
    path, _, _, service, _ = environment
    nomenclature = _nomenclature(environment)
    batch = _prepare(
        service,
        "перемещение: новый сироп 0.25 л -> кухня",
        actor=10,
        message=903,
    )
    line = batch["lines"][0]
    state = _FakeState(
        {
            "batch_id": batch["id"],
            "current_line_id": line["id"],
            "new_product_name": "Новый сироп",
            "new_product_base_unit": "l",
            "unknown_product_operation_type": "transfer",
        }
    )
    message = _FakeMessage()
    callback = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=10),
        message=message,
    )
    callback_data = SimpleNamespace(object_id=line["id"])
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    shown = AsyncMock()
    monkeypatch.setattr(warehouse_handler, "_show_batch", shown)
    before = len(nomenclature.list_products(10))

    for _ in range(2):
        asyncio.run(
            warehouse_handler.unknown_product_create_confirm(
                callback,
                callback_data,
                state,
                service,
                nomenclature,
            )
        )

    assert len(nomenclature.list_products(10)) == before + 1
    stored = service.repository.get_batch(batch["id"])
    assert stored["status"] == "ready"
    assert stored["lines"][0]["product_name"] == "Новый сироп"
    assert stored["lines"][0]["quantity_scaled"] == 250_000
    assert shown.await_count == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_products WHERE normalized_name='новый сироп'"
        ).fetchone()[0] == 1


def test_unknown_product_add_cancel_creates_nothing(environment, monkeypatch) -> None:
    _, _, _, service, _ = environment
    nomenclature = _nomenclature(environment)
    batch = _prepare(
        service,
        "перемещение: отменяемый сироп 0.25 л -> кухня",
        actor=10,
        message=904,
    )
    line = batch["lines"][0]
    state = _FakeState({"batch_id": batch["id"], "current_line_id": line["id"]})
    message = _FakeMessage()
    callback = SimpleNamespace(
        answer=AsyncMock(), from_user=SimpleNamespace(id=10), message=message
    )
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    monkeypatch.setattr(warehouse_handler, "_show_batch", AsyncMock())
    before = len(nomenclature.list_products(10))

    asyncio.run(
        warehouse_handler.unknown_product_add_cancel(
            callback, SimpleNamespace(object_id=line["id"]), state, service
        )
    )

    assert len(nomenclature.list_products(10)) == before
    assert service.repository.get_batch(batch["id"])["status"] == "needs_resolution"


def test_non_admin_cannot_create_unknown_product_and_source_is_kept(
    environment, monkeypatch
) -> None:
    _, _, _, service, _ = environment
    nomenclature = _nomenclature(environment)
    batch = _prepare(
        service,
        "перемещение: барменский сироп 0.25 л -> кухня",
        actor=20,
        message=905,
    )
    line = batch["lines"][0]
    state = _FakeState({"batch_id": batch["id"], "current_line_id": line["id"]})
    message = _FakeMessage()
    callback = SimpleNamespace(
        answer=AsyncMock(), from_user=SimpleNamespace(id=20), message=message
    )
    callback_data = SimpleNamespace(object_id=line["id"])
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    before = len(nomenclature.list_products(10))

    asyncio.run(
        warehouse_handler.unknown_product_add_start(
            callback, callback_data, state, service.access, service
        )
    )

    assert len(nomenclature.list_products(10)) == before
    assert state.data["unknown_product_text"] == "барменский сироп"
    assert state.data["unknown_product_raw_line"] == (
        "барменский сироп 0.25 л -> кухня"
    )
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "📨 Отправить запрос администратору" in {
        button.text for row in markup.inline_keyboard for button in row
    }
    with pytest.raises(AccessDeniedError):
        nomenclature.create_product(
            20,
            WarehouseProductCreate(
                official_name="Барменский сироп",
                base_unit=BaseUnit.L,
                operation_types={ProductOperationType.TRANSFER},
            ),
        )


def test_transfer_without_destination_needs_button_resolution(
    environment, monkeypatch
) -> None:
    _, _, repository, service, _ = environment
    batch = _prepare(
        service,
        "перемещение: морковь зачищ 0,33 кг",
        actor=20,
        message=910,
    )
    line = batch["lines"][0]
    assert batch["status"] == "needs_resolution"
    assert line["error_code"] == "missing_destination"
    assert line["product_name"] == "Морковь зачищ"
    assert line["quantity_scaled"] == 330_000
    assert line["base_unit"] == "kg"
    assert line["destination_text"] is None
    assert line["destination_location_id"] is None

    state, message = _FakeState(), _FakeMessage()
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 20))
    assert message.answer.await_args.args[0] == (
        "Куда переместить «Морковь зачищ» — 0,33 кг?"
    )
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert [button.text for row in markup.inline_keyboard for button in row][:2] == [
        "🍳 Кухня",
        "🍰 Кондитерка",
    ]
    assert "Бар" not in {
        button.text for row in markup.inline_keyboard for button in row
    }
    assert repository.list_operations(batch["shift_id"]) == []


def test_transfer_destination_resolution_sets_both_fields_and_is_idempotent(
    environment,
) -> None:
    _, _, repository, service, _ = environment
    batch = _prepare(
        service,
        "перемещение: сахар 1 кг (на запас)",
        actor=20,
        message=911,
    )
    line = batch["lines"][0]
    kitchen, confectionery = service.destination_options(actor_user_id=20)
    assert (kitchen.name, confectionery.name) == ("Кухня", "Кондитерка")

    resolved = service.resolve_destination(
        batch["id"], line["id"], kitchen.id, actor_user_id=20
    )
    resolved_line = resolved["lines"][0]
    assert resolved["status"] == "ready"
    assert resolved_line["destination_text"] == "Кухня"
    assert resolved_line["destination_location_id"] == kitchen.id
    again = service.resolve_destination(
        batch["id"], line["id"], kitchen.id, actor_user_id=20
    )
    assert again["lines"][0]["destination_location_id"] == kitchen.id
    assert repository.list_operations(batch["shift_id"]) == []


def test_transfer_destination_rejects_source_inactive_and_non_destination(
    environment,
) -> None:
    path, _, repository, service, _ = environment
    batch = _prepare(
        service, "перемещение: сахар 1 кг", actor=20, message=912
    )
    line = batch["lines"][0]
    source_id = int(repository.runtime_settings()["source_location_id"])
    with pytest.raises(WarehouseOperationsError, match="differ"):
        service.resolve_destination(
            batch["id"], line["id"], source_id, actor_user_id=20
        )

    kitchen, confectionery = service.destination_options(actor_user_id=20)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE warehouse_locations SET is_active=0 WHERE id=?", (kitchen.id,)
        )
        connection.execute(
            "UPDATE warehouse_locations "
            "SET can_be_source=1, can_be_destination=0 WHERE id=?",
            (confectionery.id,),
        )
        connection.commit()
    assert service.destination_options(actor_user_id=20) == ()
    for location_id in (kitchen.id, confectionery.id):
        with pytest.raises(WarehouseOperationsError, match="not found"):
            service.resolve_destination(
                batch["id"], line["id"], location_id, actor_user_id=20
            )


def test_transfer_destination_cannot_mutate_cancelled_or_committed_batch(
    environment,
) -> None:
    _, _, _, service, _ = environment
    kitchen = service.destination_options(actor_user_id=20)[0]
    cancelled = _prepare(
        service, "перемещение: сахар 1 кг", actor=20, message=913
    )
    cancelled_line = cancelled["lines"][0]
    service.cancel_batch(cancelled["id"], actor_user_id=20)
    with pytest.raises(WarehouseOperationsError, match="cannot be changed"):
        service.resolve_destination(
            cancelled["id"], cancelled_line["id"], kitchen.id, actor_user_id=20
        )

    committed = _prepare(
        service, "перемещение: сахар 1 кг -> кухня", actor=20, message=914
    )
    committed_line = committed["lines"][0]
    service.commit_batch(committed["id"], actor_user_id=20)
    with pytest.raises(WarehouseOperationsError, match="cannot be changed"):
        service.resolve_destination(
            committed["id"], committed_line["id"], kitchen.id,
            actor_user_id=20,
        )


def test_multiline_transfer_resolves_destinations_in_order_before_commit(
    environment, monkeypatch
) -> None:
    _, _, repository, service, _ = environment
    batch = _prepare(
        service,
        "перемещение:\nморковь зачищ 0,33 кг\nсахар 1 кг",
        actor=20,
        message=915,
    )
    first, second = batch["lines"]
    kitchen, confectionery = service.destination_options(actor_user_id=20)
    assert [line["error_code"] for line in batch["lines"]] == [
        "missing_destination", "missing_destination"
    ]
    state = _FakeState({"batch_id": batch["id"]})
    message = _FakeMessage()
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)

    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 20))
    assert "Морковь зачищ" in message.answer.await_args.args[0]
    first_callback = SimpleNamespace(
        answer=AsyncMock(), from_user=SimpleNamespace(id=20), message=message
    )
    asyncio.run(warehouse_handler.batch_resolve_destination(
        first_callback,
        SimpleNamespace(object_id=first["id"], aux=kitchen.id),
        state,
        service,
    ))
    assert "Сахар" in message.answer.await_args.args[0]
    assert repository.list_operations(batch["shift_id"]) == []

    second_callback = SimpleNamespace(
        answer=AsyncMock(), from_user=SimpleNamespace(id=20), message=message
    )
    asyncio.run(warehouse_handler.batch_resolve_destination(
        second_callback,
        SimpleNamespace(object_id=second["id"], aux=confectionery.id),
        state,
        service,
    ))
    stored = repository.get_batch(batch["id"])
    assert [line["destination_name"] for line in stored["lines"]] == [
        "Кухня", "Кондитерка"
    ]
    assert repository.list_operations(batch["shift_id"]) == []


def test_transfer_without_destination_preserves_other_resolution_errors(
    environment,
) -> None:
    _, _, _, service, _ = environment
    missing_unit = _prepare(
        service, "перемещение: морковь зачищ 0,33", actor=20, message=916
    )
    line = missing_unit["lines"][0]
    assert line["error_code"] == "missing_unit"
    after_unit = service.resolve_unit(
        missing_unit["id"], line["id"], "кг", actor_user_id=20
    )
    assert after_unit["lines"][0]["error_code"] == "missing_destination"
    service.cancel_batch(missing_unit["id"], actor_user_id=20)

    unknown = _prepare(
        service, "перемещение: новый продукт 0,25 л", actor=20, message=917
    )
    assert unknown["lines"][0]["error_code"] in {
        "unknown_product", "ambiguous_product"
    }
    assert unknown["lines"][0]["destination_text"] is None


def test_arrow_transfer_remains_ready_without_destination_prompt(
    environment, monkeypatch
) -> None:
    _, _, _, service, _ = environment
    batch = _prepare(
        service, "перемещение: сахар 1 кг -> кухня", actor=20, message=918
    )
    assert batch["status"] == "ready"
    assert batch["lines"][0]["error_code"] is None
    state, message = _FakeState(), _FakeMessage()
    monkeypatch.setattr(warehouse_handler, "Message", _FakeMessage)
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 20))
    assert not message.answer.await_args.args[0].startswith("Куда переместить")
