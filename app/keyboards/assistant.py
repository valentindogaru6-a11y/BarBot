"""Inline keyboards for bounded assistant candidate selection."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import AssistantCandidateCallback, AssistantProposalCallback
from app.keyboards.admin_callbacks import AdminPanelCallback
from app.keyboards.user_callbacks import UserAdminCallback
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.search_service import SearchResult
from app.keyboards.warehouse import batch_keyboard
from app.utils.button_labels import search_result_button_labels


def build_assistant_candidates_keyboard(
    results: Sequence[SearchResult],
) -> InlineKeyboardMarkup:
    """Build buttons containing only stable local catalog identifiers."""

    builder = InlineKeyboardBuilder()
    labels = search_result_button_labels(results)
    for result, label in zip(results, labels, strict=True):
        builder.button(
            text=label,
            callback_data=AssistantCandidateCallback(
                category_id=result.category.id,
                item_id=result.item.id,
                subcategory_id=(
                    result.subcategory.id if result.subcategory else "_"
                ),
            ),
        )
    builder.adjust(1)
    return builder.as_markup()


def build_assistant_proposal_keyboard(token: str, *, has_reason: bool = False) -> InlineKeyboardMarkup:
    """Build an opaque, owner-validated confirmation keyboard."""

    del has_reason
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=AssistantProposalCallback(action="c", token=token),
    )
    builder.button(
        text="❌ Отменить",
        callback_data=AssistantProposalCallback(action="x", token=token),
    )
    builder.adjust(1)
    return builder.as_markup()


_FLOW_CALLBACKS = {
    "catalog_manage": ("🛠 Открыть управление каталогом", AdminPanelCallback(action="o")),
    "users_manage": ("👥 Открыть управление сотрудниками", UserAdminCallback(action="menu")),
    "attendance_xlsx": ("📊 Выбрать период табеля", WarehouseCallback(action="tx")),
    "required_writeoffs": ("✅ Начать обязательные списания", WarehouseCallback(action="cc")),
    "warehouse_operations": ("📋 Открыть записи смены", WarehouseCallback(action="ls")),
    "warehouse_close": ("✅ Перейти к закрытию смены", WarehouseCallback(action="cs")),
    "warehouse_corrections": ("📁 Выбрать закрытый отчёт", WarehouseCallback(action="hl")),
    "report_generate": ("📄 Сформировать текущий XLSX", WarehouseCallback(action="df")),
    "reports": ("📁 Открыть отчёты", WarehouseCallback(action="hl")),
    "recipients": ("👥 Открыть получателей", WarehouseCallback(action="pr")),
    "reminders": ("🔔 Открыть напоминания", WarehouseCallback(action="mr")),
    "schedules": ("🗓 Открыть расписания", WarehouseCallback(action="sd")),
}


def build_assistant_flow_keyboard(
    action: str,
    data: dict[str, object] | None = None,
) -> InlineKeyboardMarkup:
    """Bridge natural language to an existing, authorized callback/FSM flow."""

    if action == "warehouse_batch":
        batch_id = int((data or {}).get("batch_id", 0))
        if batch_id <= 0:
            raise ValueError("Warehouse batch id is missing")
        return batch_keyboard(batch_id)
    try:
        label, callback = _FLOW_CALLBACKS[action]
    except KeyError as exc:
        raise ValueError("Unknown assistant UI flow") from exc
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=callback)
    return builder.as_markup()
