"""Static Telegram UX checks for the v5 closing workflow."""

from app.handlers.warehouse_closing import _rule_text, router
from app.keyboards.warehouse import (
    closing_final_keyboard,
    closing_not_applicable_keyboard,
    closing_requirement_keyboard,
    closing_summary_keyboard,
    required_rule_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_home_close_recovery_and_rule_controls_are_capability_aware() -> None:
    bartender = _texts(warehouse_home_keyboard(can_mutate=True))
    assert "✅ Закрыть смену" in bartender
    assert "✅ Обязательные списания" not in bartender

    admin = _texts(
        warehouse_home_keyboard(
            can_mutate=True,
            can_manage_required_rules=True,
            active_closing_session_id=17,
            shift_status="waiting_required",
        )
    )
    assert "▶️ Продолжить закрытие" in admin
    assert "✅ Закрыть смену" not in admin
    assert "✅ Обязательные списания" in admin

    accountant = _texts(warehouse_home_keyboard(can_mutate=False))
    assert "✅ Закрыть смену" not in accountant
    assert "📄 Скачать текущий Excel" in accountant
    assert "📁 История отчётов" in accountant


def test_summary_checklist_not_applicable_and_final_copy() -> None:
    assert _texts(closing_summary_keyboard()) == [
        "✅ Проверить и закрыть",
        "📋 Посмотреть записи",
        "❌ Отмена",
    ]
    coffee = _texts(
        closing_requirement_keyboard(
            1, 2, suggested_text="0,06 кг", allow_not_applicable=True
        )
    )
    assert coffee == [
        "✅ Списать 0,06 кг",
        "✏️ Указать другое количество",
        "🚫 Сегодня не списывали",
        "❌ Отменить закрытие",
    ]
    reasons = _texts(closing_not_applicable_keyboard(1, 2))
    assert "Уже списано ранее" in reasons
    assert _texts(closing_final_keyboard(1, 3))[:2] == [
        "✅ Да, завершить",
        "📋 Проверить записи",
    ]


def test_admin_rule_card_escapes_html_and_has_no_delete() -> None:
    rule = {
        "id": 4,
        "product_name": "<Молоко>",
        "question_text": "Сколько <script>?",
        "quantity_mode": "manual",
        "suggested_quantity_scaled": None,
        "base_unit": "l",
        "weekdays_mask": 127,
        "minimum_scaled": None,
        "maximum_scaled": None,
        "sort_order": 20,
        "allow_not_applicable": 1,
        "is_active": 1,
        "revision": 2,
    }
    rendered = _rule_text(rule)
    assert "&lt;Молоко&gt;" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert not any("Удал" in text for text in _texts(required_rule_keyboard(rule)))


def test_closing_router_registers_callback_and_fsm_handlers() -> None:
    assert len(router.observers["callback_query"].handlers) >= 12
    assert len(router.observers["message"].handlers) >= 4


def test_all_new_callbacks_fit_telegram_64_byte_limit() -> None:
    maximum = 9_223_372_036_854_775_807
    for action in (
        "cs", "cc", "cv", "qr", "qo", "qs", "qn", "qx", "cf",
        "n1", "n2", "n3", "n4", "rm", "rv", "rn", "zq", "zm",
        "zw", "zl", "zo", "za", "zx",
        "df", "hl", "hv", "hd", "fr", "ce",
    ):
        packed = WarehouseCallback(
            action=action, object_id=maximum, aux=maximum
        ).pack()
        assert len(packed.encode("utf-8")) <= 64
