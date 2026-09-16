"""Phase 9B.2 release, manual delivery, retry, and version UI tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.handlers.warehouse_corrections import finalize_preview_text
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.keyboards.warehouse_corrections import (
    correction_dashboard_keyboard,
    correction_delivery_confirm_keyboard,
    correction_finalize_keyboard,
    correction_recipient_keyboard,
    correction_result_keyboard,
    correction_retry_keyboard,
    correction_versions_keyboard,
)
from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.storage.warehouse_correction_database import (
    WarehouseCorrectionConflict,
    WarehouseCorrectionError,
)
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase
from tests.test_warehouse_corrections import (  # noqa: F401
    _open,
    _product,
    _recipient,
    correction_template,
    environment,
)


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [str(button.callback_data) for row in markup.inline_keyboard for button in row]


def _mutate_and_release(env, session: dict, *, actor: int = 10) -> dict:
    env["corrections"].create_operation(
        int(session["id"]),
        operation_type="writeoff",
        product_id=_product(env, "Апельсин").id,
        quantity_text="0.2",
        reason_text="проверка выпуска",
        actor_user_id=actor,
    )
    current = env["corrections"].session(int(session["id"]), actor_user_id=actor)
    return env["corrections"].release_correction(
        int(session["id"]),
        expected_revision=int(current["revision"]),
        actor_user_id=actor,
    )


class Sender:
    def __init__(self, failed_chats: set[int] | None = None) -> None:
        self.failed_chats = failed_chats or set()
        self.calls: list[dict] = []

    async def send_report(self, **kwargs) -> int:
        self.calls.append(kwargs)
        if int(kwargs["chat_id"]) in self.failed_chats:
            raise RuntimeError("telegram details must stay private")
        return 1000 + len(self.calls)


def _delivery(env, sender: Sender) -> WarehouseReportDeliveryService:
    return WarehouseReportDeliveryService(
        WarehouseDeliveryDatabase(env["path"]),
        env["reports"],
        env["access"],
        sender,
        clock=env["clock"],
    )


def test_release_button_preview_and_accountant_read_only(environment) -> None:
    _, session = _open(environment, "Причина <проверка>")
    empty = environment["corrections"].dashboard(session["id"], actor_user_id=10)
    assert "📄 Выпуск недоступен без изменений" in _texts(
        correction_dashboard_keyboard(empty)
    )
    with pytest.raises(WarehouseCorrectionError, match="no mutations"):
        environment["corrections"].finalize_preview(
            session["id"], actor_user_id=10
        )
    with pytest.raises(WarehouseCorrectionError, match="no mutations"):
        environment["corrections"].release_correction(
            session["id"], expected_revision=session["revision"], actor_user_id=10
        )

    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id,
        quantity_text="0.1", actor_user_id=10,
    )
    preview = environment["corrections"].finalize_preview(
        session["id"], actor_user_id=10
    )
    text = finalize_preview_text(preview)
    assert "v001" in text and "v002" in text
    assert "Причина &lt;проверка&gt;" in text
    assert "Списание" in text and "Перемещение" in text and "Проработка" in text
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",
            (session["shift_id"],),
        ).fetchone()[0] == 1
    assert "📄 Выпустить исправленный отчёт" in _texts(
        correction_dashboard_keyboard(
            environment["corrections"].dashboard(session["id"], actor_user_id=10)
        )
    )
    accountant = environment["corrections"].dashboard(
        session["id"], actor_user_id=30
    )
    assert all(
        "Выпустить" not in text for text in _texts(correction_dashboard_keyboard(accountant))
    )
    with pytest.raises(AccessDeniedError):
        environment["corrections"].finalize_preview(session["id"], actor_user_id=30)


def test_release_confirm_rechecks_role_revision_and_creates_nothing_on_failure(
    environment,
) -> None:
    base, session = _open(environment)
    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id,
        quantity_text="0.1", actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].release_correction(
            session["id"], expected_revision=current["revision"] - 1,
            actor_user_id=10,
        )
    environment["users"].set_role(10, Role.USER, actor_user_id=1)
    with pytest.raises(AccessDeniedError):
        environment["corrections"].release_correction(
            session["id"], expected_revision=current["revision"], actor_user_id=10
        )
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",
            (base["shift_id"],),
        ).fetchone()[0] == 1


def test_required_checklist_blocks_release_without_creating_report(environment) -> None:
    base, session = _open(environment)
    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id,
        quantity_text="0.1", actor_user_id=10,
    )
    with sqlite3.connect(environment["path"]) as connection:
        requirement = connection.execute(
            "SELECT sr.id FROM warehouse_shift_requirements sr "
            "WHERE sr.shift_id=? AND NOT EXISTS ("
            "SELECT 1 FROM warehouse_operations o WHERE o.shift_id=sr.shift_id "
            "AND o.product_id=sr.product_id AND o.operation_type=sr.operation_type "
            "AND o.base_unit=sr.base_unit AND o.is_deleted=0) "
            "ORDER BY sr.id LIMIT 1", (base["shift_id"],)
        ).fetchone()
        assert requirement is not None
        connection.execute(
            "UPDATE warehouse_shift_requirements SET status='pending',"
            "resolved_at_utc=NULL,resolved_by=NULL,resolution_reason=NULL "
            "WHERE id=?", (requirement[0],),
        )
        connection.commit()
    preview = environment["corrections"].finalize_preview(
        session["id"], actor_user_id=10
    )
    assert preview["missing"]
    assert "✅ Выпустить новую версию" not in _texts(
        correction_finalize_keyboard(
            session["id"], preview["revision"], blocked=True
        )
    )
    result = environment["corrections"].release_correction(
        session["id"], expected_revision=preview["revision"], actor_user_id=10
    )
    assert result["ready"] is False and result["missing"]
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",
            (base["shift_id"],),
        ).fetchone()[0] == 1


def test_release_result_is_completed_closed_immutable_and_old_edits_fail(
    environment,
) -> None:
    base, session = _open(environment)
    base_path = Path(environment["project"]) / str(base["relative_file_path"])
    base_bytes = base_path.read_bytes()
    released = _mutate_and_release(environment, session)
    result = environment["corrections"].result(session["id"], actor_user_id=10)
    assert released["report"]["version"] == 2
    assert result["session"]["status"] == "completed"
    assert result["session"]["shift_status"] == "closed"
    assert base_path.read_bytes() == base_bytes
    assert {row["sheet_type"] for row in released["report"]["lines"]} == {
        "writeoff", "transfer", "development"
    }
    with pytest.raises(WarehouseCorrectionError):
        environment["corrections"].create_operation(
            session["id"], operation_type="writeoff",
            product_id=_product(environment, "Апельсин").id,
            quantity_text="0.1", actor_user_id=10,
        )
    completed = environment["corrections"].dashboard(session["id"], actor_user_id=10)
    labels = _texts(correction_dashboard_keyboard(completed))
    assert "📄 Исправленный отчёт" in labels
    assert not any("Добавить" in label or "Отменить" in label for label in labels)


def test_version_history_v001_v002_v003_and_accountant_files(environment) -> None:
    base, first = _open(environment, "Первая причина")
    second_report = _mutate_and_release(environment, first)["report"]
    shift = environment["operations"].repository.shift(base["shift_id"])
    second = environment["corrections"].open_correction(
        base["shift_id"], "Вторая <причина>",
        expected_shift_revision=shift["revision"], actor_user_id=10,
    )
    third_report = _mutate_and_release(environment, second)["report"]
    history = environment["corrections"].version_history(
        second["id"], actor_user_id=30
    )
    assert [row["version"] for row in history["versions"]] == [1, 2, 3]
    assert history["versions"][1]["supersedes_report_id"] == base["id"]
    assert history["versions"][2]["supersedes_report_id"] == second_report["id"]
    assert history["versions"][2]["correction_reason"] == "Вторая <причина>"
    assert history["versions"][2]["supersedes_version"] == 2
    assert environment["corrections"].download_result(
        second["id"], actor_user_id=30
    )["report"]["id"] == third_report["id"]
    read_only = environment["corrections"].result(second["id"], actor_user_id=30)
    assert read_only["can_manage"] is False
    assert "📨 Отправить получателям" not in _texts(
        correction_result_keyboard(
            second["id"], third_report["id"],
            can_manage=False, has_failed=True,
        )
    )


def test_recipient_policy_selection_pagination_and_revalidation(environment) -> None:
    base, session = _open(environment)
    first = _recipient(environment["path"], -501, "Получал <v001>")
    second = _recipient(environment["path"], -502, "Не получал")
    inactive = _recipient(environment["path"], -503, "Неактивный")
    with sqlite3.connect(environment["path"]) as connection:
        connection.execute(
            "UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?",
            (inactive,),
        )
        connection.execute(
            "INSERT INTO warehouse_report_deliveries("
            "report_id,recipient_id,status,attempt_count,telegram_message_id,"
            "last_attempt_at_utc,delivered_at_utc,created_at_utc,updated_at_utc) "
            "VALUES(?,?,'delivered',1,77,'now','now','now','now')",
            (base["id"], first),
        )
        connection.commit()
    _mutate_and_release(environment, session)
    preview = environment["corrections"].delivery_preview(
        session["id"], actor_user_id=10
    )
    assert preview["recipient_ids"] == (first,)
    assert {row["id"] for row in preview["active_recipients"]} == {first, second}
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_report_deliveries WHERE report_id=?",
            (preview["report_id"],),
        ).fetchone()[0] == 0
    markup = correction_recipient_keyboard(
        [
            {"id": number, "title": f"Получатель {number}"}
            for number in range(1, 9)
        ],
        session_id=session["id"], selected={1, 9}, page=0, total=10,
    )
    labels = _texts(markup)
    assert labels[0].startswith("✅")
    assert "➡️" in labels and "👁 Предпросмотр" in labels
    assert "⬅️ К результату" in labels and "❌ Отмена" in labels
    with pytest.raises(WarehouseCorrectionError, match="at least one"):
        environment["corrections"].validate_delivery_selection(
            session["id"], (), actor_user_id=10
        )
    with sqlite3.connect(environment["path"]) as connection:
        connection.execute(
            "UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?", (first,)
        )
        connection.commit()
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].validate_delivery_selection(
            session["id"], (first,), actor_user_id=10
        )


def test_recipient_policy_falls_back_to_all_active(environment) -> None:
    _, session = _open(environment)
    first = _recipient(environment["path"], -601, "Первый")
    second = _recipient(environment["path"], -602, "Второй")
    inactive = _recipient(environment["path"], -603, "Третий")
    with sqlite3.connect(environment["path"]) as connection:
        connection.execute(
            "UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?",
            (inactive,),
        )
        connection.commit()
    _mutate_and_release(environment, session)
    preview = environment["corrections"].delivery_preview(
        session["id"], actor_user_id=10
    )
    assert set(preview["recipient_ids"]) == {first, second}


def test_recipient_deactivation_race_aborts_delivery_atomically(
    environment, monkeypatch,
) -> None:
    _, session = _open(environment)
    first = _recipient(environment["path"], -651, "Первый")
    second = _recipient(environment["path"], -652, "Второй")
    released = _mutate_and_release(environment, session)["report"]
    original = environment["corrections"].validate_delivery_selection

    def deactivate_after_validation(session_id, recipient_ids, *, actor_user_id):
        rows = original(
            session_id, recipient_ids, actor_user_id=actor_user_id
        )
        with sqlite3.connect(environment["path"]) as connection:
            connection.execute(
                "UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?",
                (second,),
            )
            connection.commit()
        return rows

    monkeypatch.setattr(
        environment["corrections"],
        "validate_delivery_selection",
        deactivate_after_validation,
    )
    sender = Sender()
    with pytest.raises(WarehouseCorrectionConflict, match="selection changed"):
        asyncio.run(
            environment["corrections"].deliver_correction(
                session["id"], _delivery(environment, sender),
                actor_user_id=10, recipient_ids=(first, second),
            )
        )
    assert sender.calls == []
    with sqlite3.connect(environment["path"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_report_deliveries WHERE report_id=?",
            (released["id"],),
        ).fetchone()[0] == 0


def test_manual_delivery_is_separate_idempotent_and_caption_is_safe(environment) -> None:
    base, session = _open(environment, "Причина <опасная>")
    first = _recipient(environment["path"], -701, "Первый")
    released = _mutate_and_release(environment, session)
    sender = Sender()
    delivery = _delivery(environment, sender)
    before = delivery.history(base["id"], actor_user_id=10)
    sent = asyncio.run(
        environment["corrections"].deliver_correction(
            session["id"], delivery, actor_user_id=10, recipient_ids=(first,)
        )
    )
    repeated = asyncio.run(
        environment["corrections"].deliver_correction(
            session["id"], delivery, actor_user_id=10, recipient_ids=(first,)
        )
    )
    assert sent["delivered"] == repeated["delivered"] == 1
    assert len(sender.calls) == 1
    assert "v002" in sender.calls[0]["caption"]
    assert "Исправленный отчёт" in sender.calls[0]["caption"]
    assert "&lt;опасная&gt;" in sender.calls[0]["caption"]
    assert delivery.history(base["id"], actor_user_id=10) == before
    with sqlite3.connect(environment["path"]) as connection:
        rows = connection.execute(
            "SELECT report_id,recipient_id,status FROM warehouse_report_deliveries"
        ).fetchall()
    assert rows == [(released["report"]["id"], first, "delivered")]


def test_partial_failure_retry_only_failed_and_inactive_is_skipped(environment) -> None:
    _, session = _open(environment)
    first = _recipient(environment["path"], -801, "Успех")
    second = _recipient(environment["path"], -802, "Ошибка")
    _mutate_and_release(environment, session)
    sender = Sender({-802})
    delivery = _delivery(environment, sender)
    partial = asyncio.run(
        environment["corrections"].deliver_correction(
            session["id"], delivery, actor_user_id=10,
            recipient_ids=(first, second),
        )
    )
    assert partial["delivered"] == 1 and partial["failed"] == 1
    sender.failed_chats.clear()
    retried = asyncio.run(
        environment["corrections"].retry_correction_delivery(
            session["id"], delivery, actor_user_id=10
        )
    )
    assert retried["delivered"] == 2 and retried["failed"] == 0
    assert [call["chat_id"] for call in sender.calls].count(-801) == 1
    assert [call["chat_id"] for call in sender.calls].count(-802) == 2

    # A later failed delivery whose recipient is disabled is reported as skipped.
    third = _recipient(environment["path"], -803, "Отключён")
    failing = Sender({-803})
    second_delivery = _delivery(environment, failing)
    asyncio.run(
        environment["corrections"].deliver_correction(
            session["id"], second_delivery, actor_user_id=10,
            recipient_ids=(third,),
        )
    )
    with sqlite3.connect(environment["path"]) as connection:
        connection.execute(
            "UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?", (third,)
        )
        connection.commit()
    skipped = asyncio.run(
        environment["corrections"].retry_correction_delivery(
            session["id"], second_delivery, actor_user_id=10
        )
    )
    assert skipped["skipped_inactive"] == 1
    assert len(failing.calls) == 1


def test_correction_delivery_requires_manager_and_never_uses_schedule(environment) -> None:
    base, session = _open(environment)
    recipient = _recipient(environment["path"], -901, "Получатель")
    released = _mutate_and_release(environment, session)["report"]
    sender = Sender()
    delivery = _delivery(environment, sender)
    with pytest.raises(AccessDeniedError):
        asyncio.run(
            delivery.deliver(
                released["id"], actor_user_id=20, recipient_ids=(recipient,)
            )
        )
    with pytest.raises(ValueError, match="explicit manual"):
        asyncio.run(
            delivery.deliver_scheduled(
                released["id"], recipient_ids=(recipient,)
            )
        )
    schedules = WarehouseScheduleDatabase(environment["path"])
    assert released["id"] not in {row["id"] for row in schedules.ready_reports()}
    chosen = schedules.report_for_job(
        {"report_id": None, "shift_id": base["shift_id"]}
    )
    assert chosen["id"] == base["id"]
    assert sender.calls == []


def test_recovery_preview_is_persistent_and_manager_only(environment, monkeypatch) -> None:
    _, session = _open(environment)
    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id,
        quantity_text="0.1", actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    real_build = environment["xlsx"].build_final
    monkeypatch.setattr(
        environment["xlsx"], "build_final",
        lambda _report: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    with pytest.raises(RuntimeError, match="build failed"):
        environment["corrections"].release_correction(
            session["id"], expected_revision=current["revision"], actor_user_id=10
        )
    assert environment["corrections"].recovery_preview(
        session["id"], actor_user_id=10
    )["status"] == "failed"
    with pytest.raises(AccessDeniedError):
        environment["corrections"].recovery_preview(
            session["id"], actor_user_id=30
        )
    monkeypatch.setattr(environment["xlsx"], "build_final", real_build)
    recovered = environment["corrections"].recover_correction(
        session["id"], actor_user_id=10
    )
    repeated = environment["corrections"].recover_correction(
        session["id"], actor_user_id=10
    )
    assert recovered["report"]["id"] == repeated["report"]["id"]


def test_phase_9b2_callback_payloads_fit_telegram_limit() -> None:
    maximum = 9_223_372_036_854_775_807
    actions = ("k6", "kq", "k8", "ka", "k9", "kk", "kH", "kR", "kB",
               "kT", "kL", "kV", "kS", "kD", "kY", "kZ")
    for action in actions:
        packed = WarehouseCallback(
            action=action, object_id=maximum, aux=maximum
        ).pack()
        assert len(packed.encode()) <= 64

    markups = (
        correction_finalize_keyboard(maximum, maximum),
        correction_delivery_confirm_keyboard(maximum),
        correction_retry_keyboard(maximum, confirm=True),
        correction_versions_keyboard(
            [{"id": maximum, "version": 999, "status": "ready",
              "correction_session_id": maximum}],
            session_id=maximum,
        ),
    )
    assert all(
        len(callback.encode()) <= 64
        for markup in markups for callback in _callbacks(markup)
    )
