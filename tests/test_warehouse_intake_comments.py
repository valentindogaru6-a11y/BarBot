import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import warehouse as warehouse_handler
from app.handlers.warehouse_states import WarehouseIntakeStates
from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.seed_service import build_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 8, 20, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse-comments") / "template.db"
    UserDatabase(path).initialize()
    seed = build_warehouse_seed(ROOT / "source" / "warehouse_product_candidates.json")
    WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    return path


@pytest.fixture
def environment(database_template: Path, tmp_path: Path):
    path = tmp_path / "warehouse.db"
    shutil.copyfile(database_template, path)
    users = UserService(UserDatabase(path))
    users.ensure_user(10)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    access = AuthService(frozenset({1}), users)
    service = WarehouseOperationsService(
        WarehouseOperationsDatabase(path), access, clock=FrozenClock()
    )
    return path, service


class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.state = value.state if hasattr(value, "state") else value

    async def get_state(self):
        return self.state

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=10)
        self.answer = AsyncMock()


def callback(message: FakeMessage | None = None):
    return SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=10),
        message=message or FakeMessage(),
    )


def data(action: str, object_id: int):
    return SimpleNamespace(action=action, object_id=object_id, aux=0)


def prepare(service, raw_text: str, *, message_id: int = 1) -> dict:
    return service.prepare_batch(
        actor_user_id=10,
        source_chat_id=777,
        source_message_id=message_id,
        raw_text=raw_text,
    )


def install_fake_message(monkeypatch) -> None:
    monkeypatch.setattr(warehouse_handler, "Message", FakeMessage)


async def complete_single_line(
    service,
    batch,
    state,
    message,
    *,
    comment: str | None,
) -> None:
    await warehouse_handler._show_batch(message, state, service, batch, 10)
    line_id = int(batch["lines"][0]["id"])
    if comment is None:
        await warehouse_handler.line_comment_skip(
            callback(message), data("js", line_id), state, service
        )
    else:
        await warehouse_handler.line_comment_input_start(
            callback(message), data("ja", line_id), state, service
        )
        await warehouse_handler.line_comment_text(
            FakeMessage(comment), state, service
        )
    await warehouse_handler.line_comment_confirm(
        callback(message), data("jc", line_id), state, service
    )


async def commit_batch(service, batch, state, message) -> None:
    await warehouse_handler.batch_commit(
        callback(message), data("bc", int(batch["id"])), state, service
    )


def test_writeoff_with_comment(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(
        complete_single_line(
            service, batch, state, message, comment="  испортился  "
        )
    )
    asyncio.run(commit_batch(service, batch, state, message))
    operation = service.repository.list_operations(batch["shift_id"])[0]
    assert operation["reason_text"] == "испортился"
    assert operation["comment_text"] is None


def test_writeoff_without_comment(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(complete_single_line(service, batch, state, message, comment=None))
    asyncio.run(commit_batch(service, batch, state, message))
    operation = service.repository.list_operations(batch["shift_id"])[0]
    assert operation["reason_text"] is None


def test_transfer_with_comment(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "перемещение: сахар 0.25 кг -> кухня")
    state, message = FakeState(), FakeMessage()
    asyncio.run(
        complete_single_line(
            service, batch, state, message, comment="передано кондитеру"
        )
    )
    asyncio.run(commit_batch(service, batch, state, message))
    operation = service.repository.list_operations(batch["shift_id"])[0]
    assert operation["comment_text"] == "передано кондитеру"
    assert operation["destination_location_id"] is not None


def test_transfer_without_comment(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "перемещение: сахар 0.25 кг -> кухня")
    state, message = FakeState(), FakeMessage()
    asyncio.run(complete_single_line(service, batch, state, message, comment=None))
    asyncio.run(commit_batch(service, batch, state, message))
    operation = service.repository.list_operations(batch["shift_id"])[0]
    assert operation["comment_text"] is None


def test_mixed_multiline_comments_are_queued_per_line(
    environment, monkeypatch
) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(
        service,
        "списание:\n"
        "апельсин 0.1 кг (сгнил)\n"
        "лайм 0.2 кг\n"
        "лимон 0.3 кг (простоял ночь)",
    )
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    first, second, third = batch["lines"]
    assert state.state == WarehouseIntakeStates.comment_confirm.state

    asyncio.run(
        warehouse_handler.line_comment_confirm(
            callback(message), data("jc", first["id"]), state, service
        )
    )
    assert state.state == WarehouseIntakeStates.comment_choice.state
    assert state.data["current_line_id"] == second["id"]
    asyncio.run(
        warehouse_handler.line_comment_input_start(
            callback(message), data("ja", second["id"]), state, service
        )
    )
    asyncio.run(
        warehouse_handler.line_comment_text(
            FakeMessage("использован для теста"), state, service
        )
    )
    asyncio.run(
        warehouse_handler.line_comment_confirm(
            callback(message), data("jc", second["id"]), state, service
        )
    )
    assert state.state == WarehouseIntakeStates.comment_confirm.state
    assert state.data["current_line_id"] == third["id"]
    asyncio.run(
        warehouse_handler.line_comment_confirm(
            callback(message), data("jc", third["id"]), state, service
        )
    )
    assert state.state == WarehouseIntakeStates.resolving.state
    preview = message.answer.await_args.args[0]
    assert "Комментарий: сгнил" in preview
    assert "Комментарий: использован для теста" in preview
    assert "Комментарий: простоял ночь" in preview

    asyncio.run(commit_batch(service, batch, state, message))
    operations = service.repository.list_operations(batch["shift_id"])
    assert [item["reason_text"] for item in operations] == [
        "сгнил",
        "использован для теста",
        "простоял ночь",
    ]


def test_edit_current_line_keeps_other_lines(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание:\nапельсин 0.1 кг\nлайм 0.2 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    first, second = batch["lines"]
    asyncio.run(
            warehouse_handler.line_correction_start(
                callback(message), data("cl", first["id"]), state, service
            )
    )
    asyncio.run(
        warehouse_handler.line_correction_text(
            FakeMessage("апельсин 0.25 кг"), state, service
        )
    )
    stored = service.repository.get_batch(batch["id"])
    assert stored["lines"][0]["quantity_scaled"] == 250_000
    assert stored["lines"][1]["quantity_scaled"] == 200_000
    assert state.state == WarehouseIntakeStates.comment_choice.state


def test_cancel_creates_no_operations(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    asyncio.run(
        warehouse_handler.batch_cancel(
            callback(message), data("bx", batch["id"]), state, service
        )
    )
    assert state.state is None and state.data == {}
    assert service.repository.list_operations(batch["shift_id"]) == []
    assert service.repository.get_batch(batch["id"])["status"] == "cancelled"


def test_batch_cannot_commit_before_comment_review(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))

    asyncio.run(commit_batch(service, batch, state, message))

    assert service.repository.list_operations(batch["shift_id"]) == []
    assert service.repository.get_batch(batch["id"])["status"] == "ready"
    assert "подтвердите комментарий" in message.answer.await_args.args[0]


def test_empty_command_and_long_comments_are_rejected(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    line_id = batch["lines"][0]["id"]
    asyncio.run(
        warehouse_handler.line_comment_input_start(
            callback(message), data("ja", line_id), state, service
        )
    )
    for invalid in ("   ", "/start", "x" * 501):
        invalid_message = FakeMessage(invalid)
        asyncio.run(
            warehouse_handler.line_comment_text(invalid_message, state, service)
        )
        assert invalid_message.answer.await_count == 1
        assert state.state == WarehouseIntakeStates.comment_text.state
    stored = service.repository.get_batch(batch["id"])["lines"][0]
    assert stored["reason_text"] is None


def test_repeated_callbacks_do_not_create_duplicates(environment, monkeypatch) -> None:
    path, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    line_id = batch["lines"][0]["id"]
    skip_callback = callback(message)
    asyncio.run(
        warehouse_handler.line_comment_skip(
            skip_callback, data("js", line_id), state, service
        )
    )
    asyncio.run(
        warehouse_handler.line_comment_skip(
            skip_callback, data("js", line_id), state, service
        )
    )
    confirm_callback = callback(message)
    asyncio.run(
        warehouse_handler.line_comment_confirm(
            confirm_callback, data("jc", line_id), state, service
        )
    )
    asyncio.run(
        warehouse_handler.line_comment_confirm(
            confirm_callback, data("jc", line_id), state, service
        )
    )
    asyncio.run(commit_batch(service, batch, state, message))
    asyncio.run(commit_batch(service, batch, state, message))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_operations"
        ).fetchone()[0] == 1


def test_unknown_product_and_missing_unit_still_precede_comments(environment) -> None:
    _, service = environment
    missing = prepare(service, "списание: апельсин 0.15")
    assert missing["lines"][0]["error_code"] == "missing_unit"
    service.cancel_batch(missing["id"], actor_user_id=10)
    unknown = prepare(service, "списание: совершенно новый сироп 0.15 л", message_id=2)
    assert unknown["lines"][0]["product_id"] is None
    assert unknown["lines"][0]["error_code"] in {
        "unknown_product",
        "ambiguous_product",
    }


def test_inline_comment_can_be_changed_and_deleted(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(service, "списание: апельсин 0.15 кг (старый <текст>)")
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    line_id = batch["lines"][0]["id"]
    assert state.state == WarehouseIntakeStates.comment_confirm.state
    assert "&lt;текст&gt;" in message.answer.await_args.args[0]
    assert "Хотите добавить комментарий" not in message.answer.await_args.args[0]
    texts = {
        button.text
        for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert {
        "✅ Подтвердить",
        "✏️ Изменить позицию",
        "💬 Изменить комментарий",
        "🗑 Удалить комментарий",
        "❌ Отменить",
    } == texts

    asyncio.run(
        warehouse_handler.line_comment_input_start(
            callback(message), data("jm", line_id), state, service
        )
    )
    asyncio.run(
        warehouse_handler.line_comment_text(
            FakeMessage("новый комментарий"), state, service
        )
    )
    assert service.repository.get_batch(batch["id"])["lines"][0][
        "reason_text"
    ] == "новый комментарий"
    asyncio.run(
        warehouse_handler.line_comment_delete(
            callback(message), data("jd", line_id), state, service
        )
    )
    assert service.repository.get_batch(batch["id"])["lines"][0][
        "reason_text"
    ] is None


def test_development_flow_is_unchanged(environment, monkeypatch) -> None:
    _, service = environment
    install_fake_message(monkeypatch)
    batch = prepare(
        service,
        "проработка: Новый чай\nапельсин 0.1 кг (баланс вкуса)",
    )
    state, message = FakeState(), FakeMessage()
    asyncio.run(warehouse_handler._show_batch(message, state, service, batch, 10))
    assert state.state == WarehouseIntakeStates.resolving.state
    assert "Проверьте записи" in message.answer.await_args.args[0]
    asyncio.run(commit_batch(service, batch, state, message))
    operation = service.repository.list_operations(batch["shift_id"])[0]
    assert operation["operation_type"] == "development"
    assert operation["comment_text"] == "баланс вкуса"
