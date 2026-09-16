"""Static Telegram UX checks for Phase-7 recipients and delivery."""

from app.handlers.warehouse_recipients import router
from app.keyboards.warehouse import (
    closing_final_keyboard,
    report_delivery_result_keyboard,
    report_recipient_card_keyboard,
    report_recipients_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_recipient_management_is_admin_only_on_home() -> None:
    assert "👥 Получатели отчётов" not in _texts(
        warehouse_home_keyboard(can_mutate=True)
    )
    assert "👥 Получатели отчётов" in _texts(
        warehouse_home_keyboard(
            can_mutate=True, can_manage_report_recipients=True
        )
    )


def test_zero_recipient_confirmation_never_promises_delivery() -> None:
    zero = _texts(closing_final_keyboard(
        1, 2, recipient_count=0, can_manage_recipients=True
    ))
    assert zero[0] == "✅ Закрыть и создать Excel"
    assert "👥 Настроить получателей" in zero
    active = _texts(closing_final_keyboard(1, 2, recipient_count=3))
    assert active[0] == "✅ Закрыть, создать и отправить"


def test_recipient_list_card_and_retry_controls() -> None:
    recipient = {
        "id": 7, "title": "Бухгалтерия", "is_active": 1, "revision": 3,
    }
    list_text = _texts(report_recipients_keyboard([recipient]))
    assert list_text == [
        "✅ Бухгалтерия",
        "➕ Добавить этот чат",
        "🔢 Добавить по chat ID",
        "ℹ️ Как добавить бухгалтерскую группу",
        "⬅️ К складу",
    ]
    assert _texts(report_recipient_card_keyboard(recipient))[:2] == [
        "✏️ Изменить название", "⏸ Деактивировать",
    ]
    assert "🔁 Повторить неудачные" in _texts(
        report_delivery_result_keyboard(9, has_failed=True, can_send=True)
    )
    assert "🔁 Повторить неудачные" not in _texts(
        report_delivery_result_keyboard(9, has_failed=False)
    )
    assert "🔁 Повторить неудачные" not in _texts(
        report_delivery_result_keyboard(9, has_failed=True, can_send=False)
    )
    assert "📤 Отправить активным" in _texts(
        report_delivery_result_keyboard(9, has_failed=False, can_send=True)
    )


def test_recipient_router_and_all_callbacks_fit_limit() -> None:
    assert len(router.observers["callback_query"].handlers) == 8
    assert len(router.observers["message"].handlers) == 3
    maximum = 9_223_372_036_854_775_807
    for action in (
        "pr", "pv", "pc", "pi", "ps", "pt", "pa", "px", "ds", "dh", "da"
    ):
        packed = WarehouseCallback(
            action=action, object_id=maximum, aux=maximum
        ).pack()
        assert len(packed.encode("utf-8")) <= 64
