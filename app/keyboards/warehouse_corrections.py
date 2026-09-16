"""Inline keyboards for closed-shift correction sessions."""

from math import ceil

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.warehouse_callbacks import WarehouseCallback


def correction_entry_keyboard(
    report_id: int,
    shift_revision: int,
    *,
    session_id: int | None,
    can_manage: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if session_id is not None:
        builder.button(
            text=(
                "✏️ Продолжить исправление"
                if can_manage else "👁 Исправление (только чтение)"
            ),
            callback_data=WarehouseCallback(action="kd", object_id=session_id),
        )
    elif can_manage:
        builder.button(
            text="✏️ Исправить закрытую смену",
            callback_data=WarehouseCallback(
                action="ko", object_id=report_id, aux=shift_revision
            ),
        )
    return builder.as_markup()


def correction_open_confirm_keyboard(
    report_id: int, shift_revision: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Открыть исправление",
        callback_data=WarehouseCallback(
            action="kf", object_id=report_id, aux=shift_revision
        ),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="k0", object_id=report_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_dashboard_keyboard(session: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    session_id = int(session["id"])
    revision = int(session["revision"])
    can_manage = bool(session.get("can_manage")) and session["status"] == "open"
    if can_manage:
        for action, text in (
            ("kw", "➕ Добавить списание"),
            ("kt", "🔁 Добавить перемещение"),
            ("kg", "🧪 Добавить проработку"),
        ):
            builder.button(
                text=text,
                callback_data=WarehouseCallback(
                    action=action, object_id=session_id, aux=revision
                ),
            )
    builder.button(
        text="📋 Операции",
        callback_data=WarehouseCallback(action="kl", object_id=session_id),
    )
    builder.button(
        text="📝 Изменения",
        callback_data=WarehouseCallback(action="ke", object_id=session_id),
    )
    if can_manage and not session.get("has_mutations"):
        builder.button(
            text="❌ Отменить исправление",
            callback_data=WarehouseCallback(
                action="kn", object_id=session_id, aux=revision
            ),
        )
    elif can_manage:
        builder.button(
            text="🔒 Отмена недоступна после изменений",
            callback_data=WarehouseCallback(action="kh", object_id=session_id),
        )
    if can_manage:
        builder.button(
            text="📄 Выпуск версии — следующий этап",
            callback_data=WarehouseCallback(action="kh", object_id=session_id),
        )
    builder.button(
        text="⬅️ Назад",
        callback_data=WarehouseCallback(
            action="hv", object_id=int(session["base_report_id"])
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_operations_keyboard(
    operations: list[dict], *, session_id: int, page: int, total: int,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    badges = {
        "unchanged": "•", "added": "➕", "changed": "✏️",
        "deleted": "🗑", "restored": "♻️",
    }
    for operation in operations:
        builder.button(
            text=(
                f"{badges[str(operation['change_state'])]} "
                f"{operation['product_name_snapshot']}"
            )[:64],
            callback_data=WarehouseCallback(
                action="kv", object_id=int(operation["id"]), aux=max(page, 0)
            ),
        )
    pages = max(1, ceil(total / page_size))
    if page > 0:
        builder.button(
            text="⬅️",
            callback_data=WarehouseCallback(
                action="kl", object_id=session_id, aux=page - 1
            ),
        )
    if page + 1 < pages:
        builder.button(
            text="➡️",
            callback_data=WarehouseCallback(
                action="kl", object_id=session_id, aux=page + 1
            ),
        )
    builder.button(
        text="⬅️ К исправлению",
        callback_data=WarehouseCallback(action="kd", object_id=session_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_operation_keyboard(operation: dict, *, page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    operation_id = int(operation["id"])
    revision = int(operation["revision"])
    can_manage = (
        bool(operation.get("can_manage"))
        and operation.get("session_status") == "open"
    )
    if can_manage and not operation["is_deleted"]:
        for action, text in (
            ("k1", "✏️ Изменить продукт"),
            ("k2", "⚖️ Изменить количество"),
            ("k3", "📝 Изменить причину/комментарий"),
        ):
            builder.button(
                text=text,
                callback_data=WarehouseCallback(
                    action=action, object_id=operation_id, aux=revision
                ),
            )
        if operation["operation_type"] == "transfer":
            builder.button(
                text="📍 Изменить направление",
                callback_data=WarehouseCallback(
                    action="k4", object_id=operation_id, aux=revision
                ),
            )
        if operation["operation_type"] == "development":
            builder.button(
                text="🧪 Изменить название проработки",
                callback_data=WarehouseCallback(
                    action="k5", object_id=operation_id, aux=revision
                ),
            )
        builder.button(
            text="🗑 Удалить",
            callback_data=WarehouseCallback(
                action="kx", object_id=operation_id, aux=revision
            ),
        )
    elif can_manage:
        builder.button(
            text="♻️ Восстановить",
            callback_data=WarehouseCallback(
                action="kr", object_id=operation_id, aux=revision
            ),
        )
    builder.button(
        text="⬅️ Назад",
        callback_data=WarehouseCallback(
            action="kl", object_id=int(operation["session_id"]), aux=max(page, 0)
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_candidates_keyboard(
    candidates: tuple[tuple[int, str, int], ...], *, action: str, session_id: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id, title, _score in candidates[:5]:
        builder.button(
            text=title[:64],
            callback_data=WarehouseCallback(
                action=action, object_id=session_id, aux=item_id
            ),
        )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="k0", object_id=session_id, aux=1),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_destinations_keyboard(
    locations: tuple, *, action: str, session_id: int
) -> InlineKeyboardMarkup:
    return correction_candidates_keyboard(
        tuple((item.id, item.name, 100) for item in locations),
        action=action,
        session_id=session_id,
    )


def correction_mutation_confirm_keyboard(
    action: str, object_id: int, revision: int, *, session_id: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=WarehouseCallback(
            action=action, object_id=object_id, aux=revision
        ),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=WarehouseCallback(action="k0", object_id=session_id, aux=1),
    )
    builder.adjust(1)
    return builder.as_markup()


def correction_cancel_session_keyboard(
    session_id: int, revision: int
) -> InlineKeyboardMarkup:
    return correction_mutation_confirm_keyboard(
        "km", session_id, revision, session_id=session_id
    )


__all__ = [name for name in globals() if name.startswith("correction_")]
