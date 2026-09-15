import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import router_names
from app.config import Settings
from app.handlers.warehouse import WarehouseTriggerFilter, _configured
from app.keyboards.main_menu import build_main_menu_keyboard
from app.keyboards.warehouse import (
    operation_keyboard,
    operation_list_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.models.user import Role
from app.services.warehouse.formatting import (
    aggregates_text,
    batch_preview,
    format_scaled,
    operation_text,
    split_telegram_lines,
)
from app.telegram_commands import commands_for_role


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_feature_flag_defaults_false() -> None:
    settings = Settings(_env_file=None, bot_token="test")
    assert settings.warehouse_enabled is False


def test_router_order_only_contains_warehouse_when_enabled() -> None:
    assert router_names(warehouse_enabled=False) == (
        "app.handlers.start",
        "app.handlers.catalog",
        "app.handlers.voice",
        "app.handlers.search",
        "app.handlers.admin",
    )
    assert router_names(warehouse_enabled=True)[-4:] == (
        "app.handlers.warehouse",
        "app.handlers.warehouse_closing",
        "app.handlers.warehouse_reports",
        "app.handlers.admin",
    )


def test_main_menu_uses_ready_capability_flag() -> None:
    regular = build_main_menu_keyboard(can_view_warehouse=False)
    warehouse = build_main_menu_keyboard(
        can_view_catalog=False, can_view_warehouse=True
    )
    assert "📦 Складские операции" not in _texts(regular)
    assert _texts(warehouse) == ["📦 Складские операции"]


def test_warehouse_home_is_read_only_for_accountant() -> None:
    assert _texts(warehouse_home_keyboard(can_mutate=False)) == [
        "📋 Записи текущей смены",
        "📄 Скачать текущий Excel",
        "📁 История отчётов",
        "🏠 Главное меню",
    ]
    assert "➕ Добавить списание" in _texts(
        warehouse_home_keyboard(can_mutate=True)
    )
    assert "🧪 Добавить проработку" in _texts(
        warehouse_home_keyboard(can_mutate=True)
    )


def test_commands_are_feature_and_role_aware() -> None:
    def names(role, enabled):
        return [
            item.command
            for item in commands_for_role(role, warehouse_enabled=enabled)
        ]

    for role in (Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER, Role.ACCOUNTANT):
        assert "warehouse" in names(role, True)
        assert "warehouse" not in names(role, False)
    for role in (Role.USER, Role.BLOCKED):
        assert "warehouse" not in names(role, True)


def test_all_callbacks_fit_telegram_limit_with_64_bit_values() -> None:
    maximum = 9_223_372_036_854_775_807
    for action in (
        "h", "aw", "at", "ls", "br", "bc", "be", "bx", "rp", "rd",
        "cl", "rl", "lw", "lt", "la", "ov", "ep", "eq", "en", "ed",
        "pe", "de", "ec", "dx", "dc", "ad", "dr", "dy", "ld", "eg",
        "cs", "cc", "cv", "qr", "qo", "qs", "qn", "qx", "cf",
        "n1", "n2", "n3", "n4", "rm", "rv", "rn", "zq", "zm",
        "zw", "zl", "zo", "za", "zx",
        "df", "hl", "hv", "hd", "fr", "ce",
    ):
        packed = WarehouseCallback(
            action=action, object_id=maximum, aux=maximum
        ).pack()
        assert len(packed.encode("utf-8")) <= 64


def test_trigger_filter_is_anchored_and_rejects_commands() -> None:
    check = WarehouseTriggerFilter()

    async def accepted(text):
        return await check(SimpleNamespace(text=text))

    assert asyncio.run(accepted("списание: апельсин 1"))
    assert asyncio.run(accepted("\nПеремещение:\nсахар 1 кг -> кухня"))
    assert asyncio.run(accepted("проработка: Новый чай\nлимон 0.1 кг"))
    assert asyncio.run(accepted("перемещение на кухню:\nмолоко 1 л"))
    assert not asyncio.run(accepted("нужно сделать списание: апельсин 1"))
    assert not asyncio.run(accepted("/списание: апельсин 1"))
    assert not asyncio.run(accepted("обычный текст"))
    assert not asyncio.run(accepted("сегодня была проработка нового чая"))
    assert not asyncio.run(accepted(None))


def test_formatting_uses_comma_escape_and_auto_unit_marker() -> None:
    batch = {
        "operation_type": "writeoff",
        "lines": [{
            "product_name": "<Апельсин>",
            "product_text": "<Апельсин>",
            "quantity_scaled": 150_000,
            "base_unit": "kg",
            "input_unit_text": None,
            "reason_text": "<испорчен>",
            "comment_text": None,
        }],
    }
    rendered = "\n".join(batch_preview(batch))
    assert "0,15 кг" in rendered
    assert "единица подставлена" in rendered
    assert "&lt;Апельсин&gt;" in rendered
    assert "&lt;испорчен&gt;" in rendered
    assert "<Апельсин>" not in rendered
    assert format_scaled(2_000_000, "l") == "2 л"


def test_splitter_preserves_text_and_limit() -> None:
    lines = ["a" * 3000, "b" * 3000, "c" * 3000]
    chunks = split_telegram_lines(lines)
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "\n".join(chunks) == "\n".join(lines)


def test_operation_card_hides_internal_ids_and_accountant_controls() -> None:
    operation = {
        "id": 999,
        "shift_id": 888,
        "operation_type": "transfer",
        "product_name_snapshot": "<Молоко>",
        "quantity_scaled": 250_000,
        "base_unit": "l",
        "source_location_name": "Бар",
        "destination_location_name": "Кондитерка",
        "comment_text": "для <десерта>",
        "is_deleted": 0,
        "revision": 7,
    }
    text = operation_text(operation)
    assert "999" not in text and "888" not in text
    assert "&lt;Молоко&gt;" in text and "&lt;десерта&gt;" in text
    assert "Удалить" not in _texts(operation_keyboard(operation, can_mutate=False))
    assert "🗑 Удалить" in _texts(operation_keyboard(operation, can_mutate=True))


def test_operation_card_returns_to_the_same_filtered_page() -> None:
    operation = {
        "id": 99,
        "shift_id": 7,
        "operation_type": "writeoff",
        "product_name_snapshot": "Апельсин",
        "is_deleted": 0,
        "revision": 1,
    }
    list_markup = operation_list_keyboard(
        [operation],
        shift_id=7,
        operation_type="writeoff",
        page=3,
        total=40,
    )
    callback_data = list_markup.inline_keyboard[0][0].callback_data
    assert callback_data == WarehouseCallback(
        action="ov", object_id=99, aux=3
    ).pack()

    card_markup = operation_keyboard(
        operation,
        can_mutate=False,
        return_page=3,
    )
    assert card_markup.inline_keyboard[0][0].callback_data == WarehouseCallback(
        action="lw", object_id=7, aux=3
    ).pack()


def test_development_card_has_group_title_edit_and_returns_to_list() -> None:
    operation = {
        "id": 99,
        "shift_id": 7,
        "operation_type": "development",
        "development_title": "Новый чай",
        "product_name_snapshot": "Лимон",
        "quantity_scaled": 100_000,
        "base_unit": "kg",
        "comment_text": "кислотность",
        "is_deleted": 0,
        "revision": 2,
    }
    assert "Новый чай" in operation_text(operation)
    markup = operation_keyboard(operation, can_mutate=True, return_page=2)
    assert "🧪 Переименовать всю проработку" in _texts(markup)
    assert markup.inline_keyboard[-1][0].callback_data == WarehouseCallback(
        action="ld", object_id=7, aux=2
    ).pack()


def test_transfer_aggregate_displays_group_direction() -> None:
    rendered = "\n".join(
        aggregates_text(
            [
                {
                    "operation_type": "transfer",
                    "product_name_snapshot": "Молоко",
                    "quantity_scaled": 250_000,
                    "base_unit": "l",
                    "operation_count": 1,
                    "source_location_name": "Бар",
                    "destination_location_name": "<Кухня>",
                }
            ],
            "2026-09-09",
        )
    )
    assert "Бар → &lt;Кухня&gt;" in rendered


def test_missing_products_returns_safe_configuration_message() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    service = SimpleNamespace(is_configured=lambda: False)
    result = asyncio.run(_configured(message, service, Role.ADMIN))
    assert result is False
    output = message.answer.await_args.args[0]
    assert "не настроен" in output
    assert "seed" in output
    assert "/" not in output
