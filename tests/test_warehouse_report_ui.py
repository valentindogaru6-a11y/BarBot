"""Static Telegram UX and callback safety checks for warehouse reports."""

from app.handlers.warehouse_reports import _ready_caption, router
from app.keyboards.warehouse import (
    closing_final_keyboard,
    report_card_keyboard,
    report_history_keyboard,
    report_ready_keyboard,
    report_retry_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_home_report_controls_are_available_to_read_only_role() -> None:
    text = _texts(warehouse_home_keyboard(can_mutate=False, shift_status="open"))
    assert "📄 Скачать текущий Excel" in text
    assert "📁 История отчётов" in text
    assert "✅ Закрыть смену" not in text
    closed = _texts(
        warehouse_home_keyboard(can_mutate=False, shift_status="closed")
    )
    assert "📄 Скачать текущий Excel" not in closed
    assert "📁 История отчётов" in closed


def test_empty_confirmation_uses_distinct_callback() -> None:
    normal = closing_final_keyboard(11, 7).inline_keyboard[0][0].callback_data
    empty = closing_final_keyboard(
        11, 7, allow_empty=True
    ).inline_keyboard[0][0].callback_data
    assert normal == WarehouseCallback(action="cf", object_id=11, aux=7).pack()
    assert empty == WarehouseCallback(action="ce", object_id=11, aux=7).pack()


def test_ready_history_card_and_retry_keyboards() -> None:
    reports = [
        {"id": number, "business_date": f"2026-09-{number:02d}", "version": 1}
        for number in range(1, 9)
    ]
    history = report_history_keyboard(reports, page=0, total=9)
    assert _texts(history)[:2] == [
        "01.09.2026 — версия 1 — готов",
        "02.09.2026 — версия 1 — готов",
    ]
    assert "➡️" in _texts(history)
    assert _texts(report_card_keyboard(7, return_page=2)) == [
        "📥 Скачать",
        "⬅️ Назад",
        "🏠 Главное меню",
    ]
    assert _texts(report_ready_keyboard(7)) == [
        "📄 Скачать ещё раз",
        "📁 История отчётов",
        "🏠 Главное меню",
    ]
    assert _texts(report_retry_keyboard(5))[0] == "🔄 Повторить"


def test_final_caption_contains_counts_without_internal_metadata() -> None:
    caption = _ready_caption(
        {"id": 999, "version": 1, "writeoff_count": 14, "transfer_count": 3}
    )
    assert "Списание: 14 итоговых позиций" in caption
    assert "Перемещение: 3 итоговые позиции" in caption
    assert "Версия отчёта: 1" in caption
    assert "999" not in caption
    assert "hash" not in caption.casefold()
    assert "path" not in caption.casefold()


def test_report_router_and_callbacks_are_compact() -> None:
    assert len(router.observers["callback_query"].handlers) == 5
    maximum = 9_223_372_036_854_775_807
    for action in ("df", "hl", "hv", "hd", "fr", "ce"):
        value = WarehouseCallback(
            action=action,
            object_id=maximum,
            aux=maximum,
        ).pack()
        assert len(value.encode("utf-8")) <= 64
