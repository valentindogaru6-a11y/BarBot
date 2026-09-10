"""Inline keyboards for warehouse operations."""

from math import ceil

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import MainCallback
from app.keyboards.warehouse_callbacks import WarehouseCallback


def warehouse_home_keyboard(*, can_mutate: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_mutate:
        builder.button(text="➕ Добавить списание", callback_data=WarehouseCallback(action="aw"))
        builder.button(text="🚚 Добавить перемещение", callback_data=WarehouseCallback(action="at"))
    builder.button(text="📋 Записи текущей смены", callback_data=WarehouseCallback(action="ls"))
    builder.button(text="🏠 Главное меню", callback_data=MainCallback(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def intake_prompt_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=WarehouseCallback(action="ux"))
    builder.button(text="🏠 Главное меню", callback_data=MainCallback(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def recovery_keyboard(batch_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Продолжить", callback_data=WarehouseCallback(action="br", object_id=batch_id))
    builder.button(text="🗑 Отменить", callback_data=WarehouseCallback(action="bx", object_id=batch_id))
    builder.adjust(1)
    return builder.as_markup()


def batch_keyboard(batch_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=WarehouseCallback(action="bc", object_id=batch_id))
    builder.button(text="✏️ Исправить", callback_data=WarehouseCallback(action="be", object_id=batch_id))
    builder.button(text="❌ Отменить", callback_data=WarehouseCallback(action="bx", object_id=batch_id))
    builder.adjust(1)
    return builder.as_markup()


def batch_lines_keyboard(batch: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for number, line in enumerate(batch["lines"], start=1):
        builder.button(
            text=f"{number}. {str(line.get('product_name') or line.get('product_text'))}"[:64],
            callback_data=WarehouseCallback(action="cl", object_id=int(line["id"])),
        )
    builder.button(
        text="⬅️ Назад",
        callback_data=WarehouseCallback(action="br", object_id=int(batch["id"])),
    )
    builder.adjust(1)
    return builder.as_markup()


def problem_line_keyboard(
    batch_id: int,
    line_id: int,
    candidates: list[dict],
    *,
    destinations: tuple | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if destinations is not None:
        for location in destinations[:5]:
            builder.button(
                text=location.name,
                callback_data=WarehouseCallback(action="rd", object_id=line_id, aux=location.id),
            )
    else:
        for candidate in candidates[:5]:
            builder.button(
                text=str(candidate["official_name"])[:64],
                callback_data=WarehouseCallback(
                    action="rp", object_id=line_id, aux=int(candidate["product_id"])
                ),
            )
    builder.button(text="✏️ Ввести строку заново", callback_data=WarehouseCallback(action="cl", object_id=line_id))
    builder.button(text="🗑 Убрать строку", callback_data=WarehouseCallback(action="rl", object_id=line_id))
    builder.button(text="❌ Отменить всё", callback_data=WarehouseCallback(action="bx", object_id=batch_id))
    builder.adjust(1)
    return builder.as_markup()


def records_keyboard(shift_id: int | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if shift_id is not None:
        builder.button(text="♻️ Списание", callback_data=WarehouseCallback(action="lw", object_id=shift_id))
        builder.button(text="🚚 Перемещение", callback_data=WarehouseCallback(action="lt", object_id=shift_id))
        builder.button(text="📊 Итоги", callback_data=WarehouseCallback(action="la", object_id=shift_id))
    builder.button(text="⬅️ Назад", callback_data=WarehouseCallback(action="h"))
    builder.button(text="🏠 Главное меню", callback_data=MainCallback(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def operation_list_keyboard(
    operations: list[dict],
    *,
    shift_id: int,
    operation_type: str,
    page: int,
    total: int,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for operation in operations:
        builder.button(
            text=str(operation["product_name_snapshot"])[:64],
            callback_data=WarehouseCallback(
                action="ov", object_id=int(operation["id"]), aux=page
            ),
        )
    action = "lw" if operation_type == "writeoff" else "lt"
    pages = max(1, ceil(total / page_size))
    if page > 0:
        builder.button(text="⬅️", callback_data=WarehouseCallback(action=action, object_id=shift_id, aux=page - 1))
    if page + 1 < pages:
        builder.button(text="➡️", callback_data=WarehouseCallback(action=action, object_id=shift_id, aux=page + 1))
    builder.button(text="⬅️ К записям", callback_data=WarehouseCallback(action="ls"))
    builder.adjust(1)
    return builder.as_markup()


def operation_keyboard(
    operation: dict,
    *,
    can_mutate: bool,
    return_page: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_mutate and not operation.get("is_deleted"):
        operation_id = int(operation["id"])
        revision = int(operation["revision"])
        builder.button(text="✏️ Изменить продукт", callback_data=WarehouseCallback(action="ep", object_id=operation_id, aux=revision))
        builder.button(text="⚖️ Изменить количество", callback_data=WarehouseCallback(action="eq", object_id=operation_id, aux=revision))
        builder.button(text="📝 Изменить причину/комментарий", callback_data=WarehouseCallback(action="en", object_id=operation_id, aux=revision))
        if operation["operation_type"] == "transfer":
            builder.button(text="📍 Изменить направление", callback_data=WarehouseCallback(action="ed", object_id=operation_id, aux=revision))
        builder.button(text="🗑 Удалить", callback_data=WarehouseCallback(action="dx", object_id=operation_id, aux=revision))
    builder.button(
        text="⬅️ Назад",
        callback_data=WarehouseCallback(
            action="lw" if operation["operation_type"] == "writeoff" else "lt",
            object_id=int(operation["shift_id"]),
            aux=max(return_page, 0),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def delete_keyboard(operation_id: int, revision: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=WarehouseCallback(action="dc", object_id=operation_id, aux=revision))
    builder.button(text="❌ Нет", callback_data=WarehouseCallback(action="ov", object_id=operation_id))
    builder.adjust(1)
    return builder.as_markup()


def edit_product_candidates(
    operation_id: int,
    candidates: tuple[tuple[int, str, int], ...],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product_id, name, _score in candidates[:5]:
        builder.button(
            text=name[:64],
            callback_data=WarehouseCallback(
                action="pe", object_id=operation_id, aux=product_id
            ),
        )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="ov", object_id=operation_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def edit_destination_candidates(
    operation_id: int,
    locations: tuple,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for location in locations[:5]:
        builder.button(
            text=location.name[:64],
            callback_data=WarehouseCallback(
                action="de", object_id=operation_id, aux=location.id
            ),
        )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="ov", object_id=operation_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def confirm_edit_keyboard(
    operation_id: int,
    revision: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Сохранить",
        callback_data=WarehouseCallback(
            action="ec", object_id=operation_id, aux=revision
        ),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="ov", object_id=operation_id),
    )
    builder.adjust(1)
    return builder.as_markup()
