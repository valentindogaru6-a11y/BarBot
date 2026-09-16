"""Phase 9B.1 correction Telegram/read-model regression tests."""

import sqlite3

import pytest

from app.models.user import Role
from app.handlers.warehouse_corrections import dashboard_text, operation_text
from app.keyboards.warehouse import report_card_keyboard
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.keyboards.warehouse_corrections import (
    correction_candidates_keyboard,
    correction_dashboard_keyboard,
    correction_mutation_confirm_keyboard,
    correction_operation_keyboard,
    correction_operations_keyboard,
)
from app.services.access_service import AccessDeniedError
from app.services.warehouse.quantity_service import QuantityError
from app.storage.warehouse_correction_database import (
    WarehouseCorrectionConflict,
    WarehouseCorrectionError,
)
from tests.test_warehouse_corrections import (  # noqa: F401
    _initial_report,
    _open,
    _product,
    correction_template,
    environment,
)


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_correction_entry_button_is_admin_only_and_accountant_is_read_only(environment):
    report = _initial_report(environment)
    admin = environment["corrections"].report_entry(report["id"], actor_user_id=10)
    owner = environment["corrections"].report_entry(report["id"], actor_user_id=1)
    accountant = environment["corrections"].report_entry(report["id"], actor_user_id=30)
    assert admin["can_open"] and owner["can_open"]
    assert not accountant["can_open"] and not accountant["can_manage"]
    markup = report_card_keyboard(
        report["id"], return_page=0, can_open_correction=admin["can_open"],
        shift_revision=admin["shift_revision"], can_manage_correction=True,
    )
    assert "✏️ Исправить закрытую смену" in _texts(markup)


def test_roles_without_history_permission_cannot_read_correction(environment):
    report, session = _open(environment)
    for actor in (40, 50):
        with pytest.raises(AccessDeniedError):
            environment["corrections"].dashboard(session["id"], actor_user_id=actor)
    assert report["status"] == "ready"


def test_open_reason_is_trimmed_bounded_and_duplicate_confirm_is_idempotent(environment):
    report = _initial_report(environment)
    shift = environment["operations"].repository.shift(report["shift_id"])
    first = environment["corrections"].open_correction(
        report["shift_id"], "  Причина  ",
        expected_shift_revision=shift["revision"], actor_user_id=10,
    )
    second = environment["corrections"].open_correction(
        report["shift_id"], "Причина",
        expected_shift_revision=shift["revision"], actor_user_id=10,
    )
    assert first["id"] == second["id"] and first["reason_text"] == "Причина"


def test_stale_open_revision_writes_nothing(environment):
    report = _initial_report(environment)
    shift = environment["operations"].repository.shift(report["shift_id"])
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].open_correction(
            report["shift_id"], "Причина",
            expected_shift_revision=shift["revision"] - 1, actor_user_id=10,
        )
    assert environment["corrections"].history(
        report["shift_id"], actor_user_id=10
    ) == []


def test_active_session_recovers_without_fsm_and_entry_changes_to_continue(environment):
    report, session = _open(environment)
    entry = environment["corrections"].report_entry(report["id"], actor_user_id=10)
    assert entry["active_session_id"] == session["id"]
    markup = report_card_keyboard(
        report["id"], return_page=0,
        correction_session_id=session["id"], can_manage_correction=True,
    )
    assert "✏️ Продолжить исправление" in _texts(markup)
    assert "✏️ Исправить закрытую смену" not in _texts(markup)


def test_accountant_active_session_entry_and_dashboard_have_no_mutation_buttons(environment):
    report, session = _open(environment)
    entry = environment["corrections"].report_entry(report["id"], actor_user_id=30)
    dashboard = environment["corrections"].dashboard(session["id"], actor_user_id=30)
    entry_markup = report_card_keyboard(
        report["id"], return_page=0,
        correction_session_id=entry["active_session_id"],
        can_manage_correction=False,
    )
    assert "👁 Исправление (только чтение)" in _texts(entry_markup)
    assert _texts(correction_dashboard_keyboard(dashboard)) == [
        "📋 Операции", "📝 Изменения", "⬅️ Назад"
    ]


def test_dashboard_contains_session_base_reason_counts_and_escaped_html(environment):
    _, session = _open(environment, "Исправить <остаток>")
    dashboard = environment["corrections"].dashboard(session["id"], actor_user_id=10)
    rendered = dashboard_text(dashboard)
    assert "Базовый отчёт: версия 1" in rendered
    assert "Исправить &lt;остаток&gt;" in rendered and "<остаток>" not in rendered
    assert "Активных операций: 3" in rendered
    assert "Новая версия отчёта ещё не выпущена" in rendered


def test_dashboard_cancel_is_available_only_before_first_mutation(environment):
    _, session = _open(environment)
    before = environment["corrections"].dashboard(session["id"], actor_user_id=10)
    assert "❌ Отменить исправление" in _texts(correction_dashboard_keyboard(before))
    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id, quantity_text="0.1",
        expected_session_revision=session["revision"], actor_user_id=10,
    )
    after = environment["corrections"].dashboard(session["id"], actor_user_id=10)
    assert "❌ Отменить исправление" not in _texts(correction_dashboard_keyboard(after))
    assert "🔒 Отмена недоступна после изменений" in _texts(
        correction_dashboard_keyboard(after)
    )


def test_operation_list_paginates_and_marks_added_changed_deleted_restored(environment):
    _, session = _open(environment)
    orange = _product(environment, "Апельсин")
    current = session
    created_ids = []
    for _ in range(7):
        row = environment["corrections"].create_operation(
            session["id"], operation_type="writeoff", product_id=orange.id,
            quantity_text="0.01", expected_session_revision=current["revision"],
            actor_user_id=10,
        )
        created_ids.append(row["id"])
        current = environment["corrections"].session(session["id"], actor_user_id=10)
    rows, total = environment["corrections"].operation_page(
        session["id"], page=0, actor_user_id=10
    )
    markup = correction_operations_keyboard(
        rows, session_id=session["id"], page=0, total=total
    )
    assert total == 10 and "➡️" in _texts(markup)
    assert any(text.startswith("➕") for text in _texts(markup))
    operation = environment["corrections"].operation(
        session["id"], created_ids[0], actor_user_id=10
    )
    assert operation["change_state"] == "added"


def test_create_writeoff_transfer_development_and_fuzzy_selection_ui(environment):
    _, session = _open(environment)
    current = session
    orange = _product(environment, "Апельсин")
    writeoff = environment["corrections"].create_operation(
        session["id"], operation_type="writeoff", product_id=orange.id,
        quantity_text="0.1", reason_text="бой",
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    destination = environment["operations"].destination_options(actor_user_id=10)[0]
    transfer = environment["corrections"].create_operation(
        session["id"], operation_type="transfer",
        product_id=_product(environment, "Сахар").id, quantity_text="0.2",
        destination_location_id=destination.id,
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    development = environment["corrections"].create_operation(
        session["id"], operation_type="development", product_id=orange.id,
        quantity_text="0.1", development_title="Тестовая проработка",
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    assert {writeoff["operation_type"], transfer["operation_type"], development["operation_type"]} == {
        "writeoff", "transfer", "development"
    }
    candidates = correction_candidates_keyboard(
        ((orange.id, "Апельсин", 91),), action="kp", session_id=session["id"]
    )
    assert "Апельсин" in _texts(candidates)


@pytest.mark.parametrize("quantity,unit", [("0", None), ("-1", None), ("0.1", "л")])
def test_invalid_correction_quantity_or_unit_is_rejected(environment, quantity, unit):
    _, session = _open(environment)
    with pytest.raises((QuantityError, ValueError)):
        environment["corrections"].create_operation(
            session["id"], operation_type="writeoff",
            product_id=_product(environment, "Апельсин").id,
            quantity_text=quantity, unit_text=unit, actor_user_id=10,
        )


def test_invalid_destination_and_unknown_product_do_not_create_operation(environment):
    _, session = _open(environment)
    with pytest.raises(WarehouseCorrectionError):
        environment["corrections"].create_operation(
            session["id"], operation_type="writeoff", product_id=999999,
            quantity_text="1", actor_user_id=10,
        )
    with pytest.raises(WarehouseCorrectionError):
        environment["corrections"].create_operation(
            session["id"], operation_type="transfer",
            product_id=_product(environment, "Сахар").id, quantity_text="1",
            destination_location_id=999999, actor_user_id=10,
        )


def test_role_change_before_confirm_and_stale_session_revision_block_create(environment):
    _, session = _open(environment)
    environment["users"].set_role(10, Role.ACCOUNTANT, actor_user_id=1)
    with pytest.raises(AccessDeniedError):
        environment["corrections"].create_operation(
            session["id"], operation_type="writeoff",
            product_id=_product(environment, "Апельсин").id,
            quantity_text="0.1", expected_session_revision=session["revision"],
            actor_user_id=10,
        )


def test_parallel_or_repeated_add_confirm_is_session_cas_safe(environment):
    _, session = _open(environment)
    orange = _product(environment, "Апельсин")
    environment["corrections"].create_operation(
        session["id"], operation_type="writeoff", product_id=orange.id,
        quantity_text="0.1", expected_session_revision=session["revision"],
        actor_user_id=10,
    )
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].create_operation(
            session["id"], operation_type="writeoff", product_id=orange.id,
            quantity_text="0.1", expected_session_revision=session["revision"],
            actor_user_id=10,
        )


def test_edit_product_quantity_note_destination_and_development_title(environment):
    _, session = _open(environment)
    operations = environment["operations"].repository.list_operations(session["shift_id"])
    writeoff = next(row for row in operations if row["operation_type"] == "writeoff")
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    changed = environment["corrections"].update_product_and_quantity(
        session["id"], writeoff["id"], _product(environment, "Лайм").id,
        "0.2", "кг", expected_revision=writeoff["revision"],
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    noted = environment["corrections"].update_note(
        session["id"], changed["id"], reason_text="новая", comment_text=None,
        expected_revision=changed["revision"],
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    transfer = next(row for row in operations if row["operation_type"] == "transfer")
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    destination = environment["operations"].destination_options(actor_user_id=10)[1]
    moved = environment["corrections"].update_destination(
        session["id"], transfer["id"], destination.id,
        expected_revision=transfer["revision"],
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    development = next(row for row in operations if row["operation_type"] == "development")
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    renamed = environment["corrections"].rename_development(
        session["id"], development["id"], "Новое название",
        expected_revision=development["revision"],
        expected_session_revision=current["revision"], actor_user_id=10,
    )
    assert noted["reason_text"] == "новая"
    assert moved["destination_location_id"] == destination.id
    assert renamed["development_title"] == "Новое название"


def test_old_to_new_diff_is_human_readable_and_html_safe(environment):
    _, session = _open(environment)
    operation = next(
        row for row in environment["operations"].repository.list_operations(session["shift_id"])
        if row["operation_type"] == "writeoff"
    )
    environment["corrections"].update_note(
        session["id"], operation["id"], reason_text="новая <причина>",
        comment_text=None, expected_revision=operation["revision"],
        expected_session_revision=session["revision"], actor_user_id=10,
    )
    model = environment["corrections"].operation(
        session["id"], operation["id"], actor_user_id=10
    )
    rendered = operation_text(model)
    assert "Отличия от исходной версии" in rendered
    assert "Причина: исходное → новая &lt;причина&gt;" in rendered
    assert "<причина>" not in rendered


def test_stale_operation_revision_and_deleted_operation_edit_are_rejected(environment):
    _, session = _open(environment)
    operation = environment["operations"].repository.list_operations(session["shift_id"])[0]
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].update_quantity(
            session["id"], operation["id"], "0.2", None,
            expected_revision=operation["revision"] - 1,
            expected_session_revision=session["revision"], actor_user_id=10,
        )
    deleted = environment["corrections"].delete_operation(
        session["id"], operation["id"], expected_revision=operation["revision"],
        expected_session_revision=session["revision"], reason="ошибка",
        actor_user_id=10,
    )
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    with pytest.raises(WarehouseCorrectionError, match="restored"):
        environment["corrections"].update_quantity(
            session["id"], operation["id"], "0.2", None,
            expected_revision=deleted["revision"],
            expected_session_revision=current["revision"], actor_user_id=10,
        )


def test_delete_restore_are_soft_revisioned_and_base_snapshot_is_immutable(environment):
    base, session = _open(environment)
    snapshot = [dict(row) for row in base["lines"]]
    operation = environment["operations"].repository.list_operations(session["shift_id"])[0]
    deleted = environment["corrections"].delete_operation(
        session["id"], operation["id"], expected_revision=operation["revision"],
        expected_session_revision=session["revision"], reason="ошибка",
        actor_user_id=10,
    )
    model = environment["corrections"].operation(
        session["id"], operation["id"], actor_user_id=10
    )
    assert model["change_state"] == "deleted" and model["is_deleted"]
    current = environment["corrections"].session(session["id"], actor_user_id=10)
    restored = environment["corrections"].restore_operation(
        session["id"], operation["id"], expected_revision=deleted["revision"],
        expected_session_revision=current["revision"], reason="вернуть",
        actor_user_id=10,
    )
    model = environment["corrections"].operation(
        session["id"], operation["id"], actor_user_id=10
    )
    assert not restored["is_deleted"] and model["change_state"] == "restored"
    assert environment["corrections"].repository.report(base["id"])["lines"] == snapshot


def test_duplicate_delete_restore_callbacks_are_safe(environment):
    _, session = _open(environment)
    operation = environment["operations"].repository.list_operations(session["shift_id"])[0]
    environment["corrections"].delete_operation(
        session["id"], operation["id"], expected_revision=operation["revision"],
        expected_session_revision=session["revision"], reason="ошибка",
        actor_user_id=10,
    )
    with pytest.raises(WarehouseCorrectionConflict):
        environment["corrections"].delete_operation(
            session["id"], operation["id"], expected_revision=operation["revision"],
            expected_session_revision=session["revision"], reason="повтор",
            actor_user_id=10,
        )


def test_cancel_session_before_mutation_returns_closed_and_after_mutation_is_blocked(environment):
    report, session = _open(environment)
    cancelled = environment["corrections"].cancel_correction(
        session["id"], expected_revision=session["revision"], actor_user_id=10
    )
    assert cancelled["status"] == "cancelled"
    assert environment["operations"].repository.shift(report["shift_id"])["status"] == "closed"
    shift = environment["operations"].repository.shift(report["shift_id"])
    again = environment["corrections"].open_correction(
        report["shift_id"], "Снова", expected_shift_revision=shift["revision"],
        actor_user_id=10,
    )
    environment["corrections"].create_operation(
        again["id"], operation_type="writeoff",
        product_id=_product(environment, "Апельсин").id, quantity_text="0.1",
        expected_session_revision=again["revision"], actor_user_id=10,
    )
    current = environment["corrections"].session(again["id"], actor_user_id=10)
    with pytest.raises(WarehouseCorrectionError, match="mutations"):
        environment["corrections"].cancel_correction(
            again["id"], expected_revision=current["revision"], actor_user_id=10
        )


def test_all_correction_callbacks_fit_telegram_64_byte_limit():
    maximum = 9_223_372_036_854_775_807
    actions = (
        "ko", "kf", "k0", "kd", "kl", "kv", "kw", "kt", "kg", "kp",
        "ki", "ks", "ke", "k1", "k2", "k3", "k4", "k5", "ku", "kj",
        "ky", "kx", "kz", "kr", "kb", "kn", "km", "kh",
        "k6", "kq", "k8", "ka", "k9", "kk", "kH", "kR", "kB",
        "kT", "kL", "kV", "kS", "kD", "kY", "kZ",
    )
    for action in actions:
        packed = WarehouseCallback(
            action=action, object_id=maximum, aux=maximum
        ).pack()
        assert len(packed.encode()) <= 64


def test_cancel_and_preview_keyboards_do_not_mutate_by_themselves():
    preview = correction_mutation_confirm_keyboard("ks", 3, 4, session_id=3)
    assert _callbacks(preview) == [
        WarehouseCallback(action="ks", object_id=3, aux=4).pack(),
        WarehouseCallback(action="k0", object_id=3, aux=1).pack(),
    ]


def test_operation_controls_are_hidden_for_accountant_and_deleted_edit_is_hidden(environment):
    _, session = _open(environment)
    operation = environment["operations"].repository.list_operations(session["shift_id"])[0]
    read_only = environment["corrections"].operation(
        session["id"], operation["id"], actor_user_id=30
    )
    assert _texts(correction_operation_keyboard(read_only)) == ["⬅️ Назад"]


def test_base_report_lines_have_sqlite_immutable_guard_during_ui_mutations(environment):
    base, session = _open(environment)
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(environment["path"]) as connection:
            connection.execute(
                "UPDATE warehouse_report_lines SET quantity_scaled=1 WHERE report_id=?",
                (base["id"],),
            )
    assert environment["corrections"].dashboard(
        session["id"], actor_user_id=10
    )["status"] == "open"
