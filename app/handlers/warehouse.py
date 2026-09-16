"""Telegram UI for warehouse write-offs, transfers, and current-shift records."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import (
    WarehouseIntakeStates,
    WarehouseOperationEditStates,
)
from app.keyboards.warehouse import (
    batch_keyboard,
    batch_lines_keyboard,
    confirm_edit_keyboard,
    delete_keyboard,
    development_draft_keyboard,
    edit_destination_candidates,
    edit_product_candidates,
    intake_prompt_keyboard,
    operation_keyboard,
    operation_list_keyboard,
    problem_line_keyboard,
    records_keyboard,
    recovery_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.models.user import Role
from app.models.warehouse.operations import WarehouseOperationType
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.warehouse.formatting import (
    aggregates_text,
    batch_preview,
    format_business_date,
    format_scaled,
    operation_text,
    operations_list_text,
    split_telegram_lines,
)
from app.services.warehouse.intake_parser import (
    IntakeParseError,
    is_warehouse_trigger_header,
    parse_intake_message,
)
from app.services.warehouse.operations_service import (
    WarehouseIntakeError,
    WarehouseOperationsService,
)
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.quantity_service import QuantityError, parse_quantity
from app.storage.warehouse_operations_database import (
    WarehouseOperationsError,
    WarehouseRevisionConflict,
)

router = Router(name=__name__)
logger = logging.getLogger(__name__)

WAREHOUSE_TITLE = "📦 <b>Складские операции</b>"
WRITEOFF_PROMPT = (
    "Отправьте одну или несколько позиций, каждую с новой строки.\n\n"
    "Пример:\nапельсин 0.15\nморковь 300 г (сгнила)\n\n"
    "Единицу можно не указывать — тогда будет использована основная "
    "единица продукта."
)
TRANSFER_PROMPT = (
    "Отправьте одну или несколько позиций, каждую с новой строки.\n\n"
    "Пример:\nкокосовое молоко 0.25 л -> кондитерка\n"
    "сахар 1 кг -> кухня"
)
DEVELOPMENT_BODY_PROMPT = (
    "Отправьте ингредиенты проработки построчно.\n\n"
    "Пример:\nоблепиха 0.25 кг\nпюре маракуйя 0.1 кг\n"
    "сироп сахарный 0.15 л"
)


class WarehouseTriggerFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.lstrip().startswith("/"):
            return False
        first = next(
            (line.strip() for line in message.text.splitlines() if line.strip()),
            "",
        )
        return is_warehouse_trigger_header(first)


def _error_text(error: Exception) -> str:
    value = str(error).casefold()
    if isinstance(error, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(error, WarehouseRevisionConflict):
        return "Запись уже изменена другим сотрудником. Открыта актуальная версия."
    if "not open" in value:
        return "Смена уже закрыта, изменение невозможно."
    if "not ready" in value:
        return "Эта запись уже подтверждена или больше не готова к сохранению."
    if "belongs to another" in value:
        return "Эта незавершённая запись принадлежит другому сотруднику."
    if isinstance(error, QuantityError):
        return str(error)
    if isinstance(error, WarehouseIntakeError):
        detail = str(error).split(":", 1)[-1].strip()
        return f"Не удалось разобрать сообщение: {escape(detail)}."
    return "Временная ошибка склада. Попробуйте ещё раз."


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


async def _send_chunks(
    message: Message,
    chunks: list[str],
    reply_markup=None,
) -> None:
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


async def _configured(
    message: Message,
    service: WarehouseOperationsService,
    role: Role,
) -> bool:
    try:
        ready = await _thread(service.is_configured)
    except Exception:
        logger.exception("Warehouse configuration check failed")
        await message.answer("Модуль склада временно недоступен.")
        return False
    if ready:
        return True
    text = "Модуль склада ещё не настроен."
    if role in {Role.SUPER_ADMIN, Role.ADMIN}:
        text += " Выполните отдельный импорт warehouse seed."
    await message.answer(text)
    return False


async def show_warehouse_home(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    service: WarehouseOperationsService,
    user_id: int,
    closing_service: WarehouseClosingService | None = None,
) -> None:
    context = await _thread(auth_service.get_context, user_id)
    if not context.can_view_warehouse:
        await message.answer("Недостаточно прав.")
        return
    await state.clear()
    if not await _configured(message, service, context.role):
        return
    shift = await _thread(service.current_shift, actor_user_id=user_id)
    closing_overview = None
    if closing_service is not None:
        try:
            closing_overview = await _thread(
                closing_service.overview, actor_user_id=user_id
            )
            shift = closing_overview["shift"]
        except Exception:
            logger.exception("Warehouse closing overview failed")
    active_closing = None
    if closing_service is not None and shift["id"] is not None:
        try:
            active_closing = await _thread(
                closing_service.active_session, actor_user_id=user_id
            )
        except Exception:
            logger.exception("Warehouse closing recovery check failed")
    if context.can_mutate_warehouse and active_closing is None:
        pending = await _thread(service.pending_intake, actor_user_id=user_id)
        if pending is not None:
            is_title_draft = bool(pending.get("needs_development_title"))
            is_ingredients_draft = bool(
                pending.get("needs_development_ingredients")
            )
            await message.answer(
                (
                    "У вас есть проработка без названия."
                    if is_title_draft
                    else "У вас есть незавершённая проработка."
                    if is_ingredients_draft
                    else "У вас есть незавершённая запись."
                ),
                reply_markup=(
                    development_draft_keyboard(
                        int(pending["id"]),
                        needs_ingredients=is_ingredients_draft,
                    )
                    if is_title_draft or is_ingredients_draft
                    else recovery_keyboard(int(pending["id"]))
                ),
            )
            return
    status_label = {
        "open": "открыта",
        "waiting_required": "идёт проверка",
        "closing": "ожидает отчёта",
    }.get(str(shift["status"]), "закрыта")
    requirements_text = ""
    if closing_overview is not None:
        requirements_text = (
            "\nОбязательные позиции: "
            f"{closing_overview['requirements_resolved']} из "
            f"{closing_overview['requirements_total']} заполнено"
        )
    await edit_or_send_text(
        message,
        f"{WAREHOUSE_TITLE}\n\n"
        f"Смена: {format_business_date(str(shift['business_date']))}\n"
        f"Статус: {status_label}{requirements_text}\n\nВыберите действие:",
        warehouse_home_keyboard(
            can_mutate=context.can_mutate_warehouse,
            shift_status=str(shift["status"]),
            active_closing_session_id=(
                int(active_closing["id"]) if active_closing is not None else None
            ),
            can_manage_required_rules=(
                context.can_manage_warehouse_required_rules
            ),
            can_manage_report_recipients=(
                context.can_manage_warehouse_report_recipients
            ),
            can_manage_reminders=context.can_manage_warehouse_reminders,
        ),
    )


async def _show_batch(
    message: Message,
    state: FSMContext,
    service: WarehouseOperationsService,
    batch: dict,
    user_id: int,
) -> None:
    if batch["status"] == "cancelled":
        await state.clear()
        await message.answer("Незавершённая запись отменена.")
        return
    problem = next(
        (line for line in batch["lines"] if line["status"] != "resolved"),
        None,
    )
    if problem is None:
        await state.set_state(WarehouseIntakeStates.resolving)
        await state.update_data(batch_id=int(batch["id"]), current_line_id=0)
        await _send_chunks(
            message,
            batch_preview(batch),
            batch_keyboard(int(batch["id"])),
        )
        return
    await state.set_state(WarehouseIntakeStates.resolving)
    await state.update_data(
        batch_id=int(batch["id"]),
        current_line_id=int(problem["id"]),
    )
    raw = escape(str(problem["raw_line"]))
    candidates = problem.get("candidates") or []
    if candidates:
        await message.answer(
            f"Не удалось точно определить продукт:\n\n«{raw}»\n\n"
            "Выберите продукт:",
            reply_markup=problem_line_keyboard(
                int(batch["id"]), int(problem["id"]), candidates
            ),
        )
        return
    if problem.get("destination_text") and problem.get("product_id"):
        destinations = await _thread(service.destination_options, actor_user_id=user_id)
        await message.answer(
            f"Не удалось точно определить место:\n\n«{raw}»\n\n"
            "Выберите место:",
            reply_markup=problem_line_keyboard(
                int(batch["id"]),
                int(problem["id"]),
                [],
                destinations=destinations,
            ),
        )
        return
    reasons = {
        "invalid_line": "не найдено корректное количество",
        "invalid_transfer_arrow": "нужна ровно одна стрелка -> и место назначения",
        "invalid_destination": "не указано корректное место назначения",
        "header_destination_conflict": (
            "место уже указано в заголовке — уберите стрелку из строки"
        ),
        "malformed_parentheses": "комментарий должен быть в одной паре скобок в конце",
        "incompatible_unit": "единица несовместима с основной единицей продукта",
        "unsupported_unit": "единица измерения не поддерживается",
        "invalid_quantity": "не найдено корректное количество",
        "unknown_product": "продукт не найден",
    }
    reason = reasons.get(str(problem.get("error_code")), "строка содержит ошибку")
    await message.answer(
        f"Не удалось разобрать строку:\n\n«{raw}»\n\n"
        f"Причина: {escape(reason)}.\n\nОтправьте исправленную строку целиком.",
        reply_markup=problem_line_keyboard(
            int(batch["id"]), int(problem["id"]), []
        ),
    )


async def _prepare(
    message: Message,
    state: FSMContext,
    service: WarehouseOperationsService,
    raw_text: str,
) -> None:
    if message.from_user is None:
        return
    try:
        batch = await _thread(
            service.prepare_batch,
            actor_user_id=message.from_user.id,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            raw_text=raw_text,
        )
    except (WarehouseIntakeError, WarehouseOperationsError, AccessDeniedError) as exc:
        await message.answer(_error_text(exc))
        return
    except Exception:
        logger.exception("Warehouse batch preparation failed")
        await message.answer("Временная ошибка базы. Попробуйте позже.")
        return
    if batch.get("needs_development_title") or batch.get(
        "needs_development_ingredients"
    ):
        needs_ingredients = bool(batch.get("needs_development_ingredients"))
        await state.set_state(
            WarehouseIntakeStates.development_body
            if needs_ingredients
            else WarehouseIntakeStates.missing_development_title
        )
        await state.update_data(development_draft_id=int(batch["id"]))
        await message.answer(
            (
                DEVELOPMENT_BODY_PROMPT
                if needs_ingredients
                else "🧪 Укажите название проработки (до 200 символов)."
            ),
            reply_markup=development_draft_keyboard(
                int(batch["id"]), needs_ingredients=needs_ingredients
            ),
        )
        return
    await _show_batch(message, state, service, batch, message.from_user.id)


@router.message(Command("warehouse"))
async def warehouse_command(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is not None:
        await show_warehouse_home(
            message, state, auth_service, warehouse_service, message.from_user.id,
            warehouse_closing_service,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "h"))
async def warehouse_home(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    context = await _thread(auth_service.get_context, callback.from_user.id)
    if not context.can_view_warehouse:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_warehouse_home(
            callback.message, state, auth_service, warehouse_service,
            callback.from_user.id,
            warehouse_closing_service,
        )


@router.callback_query(WarehouseCallback.filter(F.action.in_({"aw", "at", "ad"})))
async def intake_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    context = await _thread(auth_service.get_context, callback.from_user.id)
    if not context.can_mutate_warehouse:
        await callback.answer("Доступ только для чтения", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message) and not await _configured(
        callback.message, warehouse_service, context.role
    ):
        return
    pending = await _thread(
        warehouse_service.pending_intake, actor_user_id=callback.from_user.id
    )
    if isinstance(callback.message, Message) and pending is not None:
        await callback.message.answer(
            "У вас есть незавершённая запись.",
            reply_markup=(
                development_draft_keyboard(
                    int(pending["id"]),
                    needs_ingredients=bool(
                        pending.get("needs_development_ingredients")
                    ),
                )
                if pending.get("needs_development_title")
                or pending.get("needs_development_ingredients")
                else recovery_keyboard(int(pending["id"]))
            ),
        )
        return
    state_value = {
        "aw": WarehouseIntakeStates.writeoff,
        "at": WarehouseIntakeStates.transfer,
        "ad": WarehouseIntakeStates.development_title,
    }[callback_data.action]
    await state.set_state(state_value)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            (
                WRITEOFF_PROMPT
                if callback_data.action == "aw"
                else TRANSFER_PROMPT
                if callback_data.action == "at"
                else "🧪 Введите название проработки (до 200 символов)."
            ),
            reply_markup=intake_prompt_keyboard(),
        )


@router.message(Command("cancel"), StateFilter(WarehouseIntakeStates))
@router.message(Command("cancel"), StateFilter(WarehouseOperationEditStates))
async def warehouse_cancel_command(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    try:
        pending = await _thread(
            warehouse_service.pending_intake, actor_user_id=message.from_user.id
        )
    except AccessDeniedError:
        pending = None
    if pending:
        await message.answer(
            "Действие закрыто, но незавершённая запись сохранена.",
            reply_markup=(
                development_draft_keyboard(
                    int(pending["id"]),
                    needs_ingredients=bool(
                        pending.get("needs_development_ingredients")
                    ),
                )
                if pending.get("needs_development_title")
                or pending.get("needs_development_ingredients")
                else recovery_keyboard(int(pending["id"]))
            ),
        )
    else:
        await message.answer("Действие отменено.")


@router.callback_query(WarehouseCallback.filter(F.action == "ux"))
async def intake_ui_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_warehouse_home(
            callback.message, state, auth_service, warehouse_service,
            callback.from_user.id,
            warehouse_closing_service,
        )


@router.message(WarehouseIntakeStates.writeoff, F.text)
@router.message(WarehouseIntakeStates.transfer, F.text)
async def intake_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    current = await state.get_state()
    trigger = (
        "списание:" if current == WarehouseIntakeStates.writeoff.state
        else "перемещение:"
    )
    await _prepare(message, state, warehouse_service, f"{trigger}\n{message.text or ''}")


@router.message(WarehouseIntakeStates.development_title, F.text)
async def development_title_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    title = (message.text or "").strip()
    if not title or len(title) > 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return
    try:
        draft = await _thread(
            warehouse_service.start_development_draft,
            title,
            actor_user_id=message.from_user.id,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
    except Exception as exc:
        await message.answer(_error_text(exc))
        return
    if draft.get("needs_development_title"):
        await state.set_state(WarehouseIntakeStates.missing_development_title)
        await state.update_data(development_draft_id=int(draft["id"]))
        await message.answer("Сначала завершите предыдущую проработку.")
        return
    await state.set_state(WarehouseIntakeStates.development_body)
    await state.update_data(development_draft_id=int(draft["id"]))
    await message.answer(DEVELOPMENT_BODY_PROMPT, reply_markup=intake_prompt_keyboard())


@router.message(WarehouseIntakeStates.development_body, F.text)
async def development_body_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    if message.from_user is None:
        return
    try:
        batch = await _thread(
            warehouse_service.complete_development_ingredients,
            int(data.get("development_draft_id", 0)),
            message.text or "",
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_error_text(exc))
        return
    await _show_batch(message, state, warehouse_service, batch, message.from_user.id)


@router.callback_query(WarehouseCallback.filter(F.action == "dr"))
async def development_draft_resume(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    pending = await _thread(
        warehouse_service.pending_intake,
        actor_user_id=callback.from_user.id,
    )
    needs_ingredients = bool(
        pending
        and int(pending["id"]) == callback_data.object_id
        and pending.get("needs_development_ingredients")
    )
    await state.set_state(
        WarehouseIntakeStates.development_body
        if needs_ingredients
        else WarehouseIntakeStates.missing_development_title
    )
    await state.update_data(development_draft_id=callback_data.object_id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            DEVELOPMENT_BODY_PROMPT
            if needs_ingredients
            else "🧪 Укажите название проработки (до 200 символов)."
        )


@router.message(WarehouseIntakeStates.missing_development_title, F.text)
async def missing_development_title_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.complete_development_title,
            int(data.get("development_draft_id", 0)),
            message.text or "",
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_error_text(exc))
        return
    await _show_batch(message, state, warehouse_service, batch, message.from_user.id)


@router.callback_query(WarehouseCallback.filter(F.action == "dy"))
async def development_draft_cancel(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        await _thread(
            warehouse_service.cancel_development_draft,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Незавершённая проработка отменена.")


@router.callback_query(WarehouseCallback.filter(F.action == "br"))
async def batch_resume(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        batch = await _thread(
            warehouse_service.owned_batch,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, WarehouseOperationsError):
        batch = None
    if batch is None:
        if isinstance(callback.message, Message):
            await callback.message.answer("Незавершённая запись недоступна.")
        return
    if isinstance(callback.message, Message):
        await _show_batch(
            callback.message, state, warehouse_service, batch, callback.from_user.id
        )


@router.callback_query(WarehouseCallback.filter(F.action == "rp"))
async def batch_resolve_product(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    batch_id = int(data.get("batch_id", 0))
    try:
        batch = await _thread(
            warehouse_service.resolve_product,
            batch_id,
            callback_data.object_id,
            callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        await _show_batch(
            callback.message, state, warehouse_service, batch, callback.from_user.id
        )


@router.callback_query(WarehouseCallback.filter(F.action == "rd"))
async def batch_resolve_destination(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.resolve_destination,
            int(data.get("batch_id", 0)),
            callback_data.object_id,
            callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        await _show_batch(
            callback.message, state, warehouse_service, batch, callback.from_user.id
        )


@router.callback_query(WarehouseCallback.filter(F.action == "cl"))
async def line_correction_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.set_state(WarehouseIntakeStates.correcting_line)
    await state.update_data(
        batch_id=int(data.get("batch_id", 0)),
        current_line_id=callback_data.object_id,
    )
    if isinstance(callback.message, Message):
        await callback.message.answer("Отправьте исправленную строку целиком.")


@router.message(WarehouseIntakeStates.correcting_line, F.text)
async def line_correction_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.replace_line_input,
            int(data["batch_id"]),
            int(data["current_line_id"]),
            message.text or "",
            actor_user_id=message.from_user.id,
        )
    except (WarehouseIntakeError, WarehouseOperationsError) as exc:
        await message.answer(_error_text(exc))
        return
    await _show_batch(message, state, warehouse_service, batch, message.from_user.id)


@router.callback_query(WarehouseCallback.filter(F.action == "rl"))
async def line_remove(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.remove_line,
            int(data.get("batch_id", 0)),
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        await _show_batch(
            callback.message, state, warehouse_service, batch, callback.from_user.id
        )


@router.callback_query(WarehouseCallback.filter(F.action == "be"))
async def batch_edit_lines(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        batch = await _thread(
            warehouse_service.owned_batch,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, WarehouseOperationsError):
        batch = None
    if batch is None:
        return
    await state.set_state(WarehouseIntakeStates.resolving)
    await state.update_data(batch_id=int(batch["id"]))
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Выберите строку для исправления:",
            reply_markup=batch_lines_keyboard(batch),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "bx"))
async def batch_cancel(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        await _thread(
            warehouse_service.cancel_batch,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Незавершённая запись отменена.")


@router.callback_query(WarehouseCallback.filter(F.action == "bc"))
async def batch_commit(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        operations = await _thread(
            warehouse_service.commit_batch,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    kind = {
        "writeoff": "Списание",
        "transfer": "Перемещение",
        "development": "Проработка",
    }[str(operations[0]["operation_type"])]
    lines = [f"✅ <b>{kind} сохранено</b>", ""]
    lines.extend(
        f"• {escape(str(item['product_name_snapshot']))} — "
        f"{format_scaled(int(item['quantity_scaled']), str(item['base_unit']))}"
        for item in operations
    )
    count = len(
        await _thread(
            warehouse_service.list_operations,
            int(operations[0]["shift_id"]),
            actor_user_id=callback.from_user.id,
        )
    )
    totals = await _thread(
        warehouse_service.aggregates,
        int(operations[0]["shift_id"]),
        actor_user_id=callback.from_user.id,
    )
    for item in operations:
        total = next(
            (
                row for row in totals
                if row["operation_type"] == item["operation_type"]
                and row["product_id"] == item["product_id"]
                and row["source_location_id"] == item["source_location_id"]
                and row["destination_location_id"] == item["destination_location_id"]
                and row.get("development_group_id")
                == item.get("development_group_id")
            ),
            None,
        )
        if total is not None and int(total["operation_count"]) > 1:
            name = escape(str(item["product_name_snapshot"]))
            lines.extend(
                (
                    "",
                    f"{name}: добавлено "
                    f"{format_scaled(int(item['quantity_scaled']), str(item['base_unit']))}",
                    "Итого за смену: "
                    f"{format_scaled(int(total['quantity_scaled']), str(total['base_unit']))}",
                )
            )
    lines.extend(("", f"Записей в текущей смене: {count}"))
    await _send_chunks(callback.message, split_telegram_lines(lines))


async def _show_records(
    message: Message,
    service: WarehouseOperationsService,
    user_id: int,
) -> None:
    shift = await _thread(service.current_shift, actor_user_id=user_id)
    operations = (
        await _thread(service.list_operations, int(shift["id"]), actor_user_id=user_id)
        if shift["id"] is not None else []
    )
    writeoffs = sum(item["operation_type"] == "writeoff" for item in operations)
    transfers = sum(item["operation_type"] == "transfer" for item in operations)
    developments = sum(item["operation_type"] == "development" for item in operations)
    development_groups = len(
        {
            int(item["development_group_id"])
            for item in operations
            if item["operation_type"] == "development"
        }
    )
    await edit_or_send_text(
        message,
        "📋 <b>Записи текущей смены</b>\n"
        f"{format_business_date(str(shift['business_date']))}\n\n"
        f"♻️ Списание: {writeoffs} записей\n"
        f"🚚 Перемещение: {transfers} записей\n"
        f"🧪 Проработка: {developments} позиций в "
        f"{development_groups} проработках",
        records_keyboard(shift["id"]),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "ls"))
async def records_home(
    callback: CallbackQuery,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await _show_records(
                callback.message, warehouse_service, callback.from_user.id
            )
        except Exception as exc:
            await callback.message.answer(_error_text(exc))


@router.callback_query(WarehouseCallback.filter(F.action.in_({"lw", "lt", "ld"})))
async def operation_list(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    kind = {
        "lw": WarehouseOperationType.WRITEOFF,
        "lt": WarehouseOperationType.TRANSFER,
        "ld": WarehouseOperationType.DEVELOPMENT,
    }[callback_data.action]
    try:
        shift = await _thread(
            warehouse_service.shift_by_id,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if shift is None:
            raise WarehouseOperationsError("Shift not found")
        rows, total = await _thread(
            warehouse_service.operation_page,
            callback_data.object_id,
            kind,
            page=max(callback_data.aux, 0),
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        await state.update_data(
            warehouse_return_page=max(callback_data.aux, 0),
            warehouse_return_type=kind.value,
        )
        await edit_or_send_text(
            callback.message,
            operations_list_text(
                rows,
                operation_type=kind.value,
                offset=max(callback_data.aux, 0) * 8,
                business_date=str(shift["business_date"]),
            ),
            operation_list_keyboard(
                rows,
                shift_id=callback_data.object_id,
                operation_type=kind.value,
                page=max(callback_data.aux, 0),
                total=total,
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "la"))
async def aggregate_list(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        shift = await _thread(
            warehouse_service.shift_by_id,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        rows = await _thread(
            warehouse_service.aggregates,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if shift is None:
            raise WarehouseOperationsError("Shift not found")
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        await _send_chunks(
            callback.message,
            aggregates_text(rows, str(shift["business_date"])),
            records_keyboard(callback_data.object_id),
        )


async def _show_operation(
    message: Message,
    service: WarehouseOperationsService,
    auth_service: AuthService,
    operation_id: int,
    user_id: int,
    *,
    return_page: int = 0,
) -> None:
    operation = await _thread(
        service.operation_view, operation_id, actor_user_id=user_id
    )
    if operation is None or operation["is_deleted"]:
        await message.answer("Запись не найдена или уже удалена.")
        return
    context = await _thread(auth_service.get_context, user_id)
    await edit_or_send_text(
        message,
        operation_text(operation),
        operation_keyboard(
            operation,
            can_mutate=(
                context.can_mutate_warehouse
                and operation["shift_status"] in {"open", "waiting_required"}
            ),
            return_page=return_page,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "ov"))
async def operation_card(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    return_page = callback_data.aux
    if "warehouse_return_page" in data:
        return_page = int(data["warehouse_return_page"])
    await state.clear()
    await state.update_data(warehouse_return_page=return_page)
    if isinstance(callback.message, Message):
        try:
            await _show_operation(
                callback.message, warehouse_service, auth_service,
                callback_data.object_id, callback.from_user.id,
                return_page=return_page,
            )
        except Exception as exc:
            await callback.message.answer(_error_text(exc))


async def _start_operation_edit(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    target_state,
    prompt: str,
) -> bool:
    context = await _thread(auth_service.get_context, callback.from_user.id)
    if not context.can_mutate_warehouse:
        await callback.answer("Доступ только для чтения", show_alert=True)
        return False
    await callback.answer()
    await state.set_state(target_state)
    await state.update_data(
        operation_id=callback_data.object_id,
        revision=callback_data.aux,
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)
    return True


@router.callback_query(WarehouseCallback.filter(F.action == "ep"))
async def edit_product_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _start_operation_edit(
        callback, callback_data, state, auth_service,
        WarehouseOperationEditStates.product,
        "Введите новое название продукта.",
    )


@router.callback_query(WarehouseCallback.filter(F.action == "eq"))
async def edit_quantity_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _start_operation_edit(
        callback, callback_data, state, auth_service,
        WarehouseOperationEditStates.quantity,
        "Введите новое количество, например 0.35 или 350 г.",
    )


@router.callback_query(WarehouseCallback.filter(F.action == "en"))
async def edit_note_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _start_operation_edit(
        callback, callback_data, state, auth_service,
        WarehouseOperationEditStates.note,
        "Введите новый текст. Отправьте «-», чтобы очистить.",
    )


@router.callback_query(WarehouseCallback.filter(F.action == "ed"))
async def edit_destination_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _start_operation_edit(
        callback, callback_data, state, auth_service,
        WarehouseOperationEditStates.destination,
        "Введите новое место назначения.",
    )


@router.callback_query(WarehouseCallback.filter(F.action == "eg"))
async def edit_development_title_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _start_operation_edit(
        callback,
        callback_data,
        state,
        auth_service,
        WarehouseOperationEditStates.development_title,
        "Введите новое название всей проработки (до 200 символов).",
    )


@router.message(WarehouseOperationEditStates.product, F.text)
async def edit_product_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    operation = await _thread(
        warehouse_service.operation_view,
        int(data["operation_id"]),
        actor_user_id=message.from_user.id,
    )
    if operation is None:
        await message.answer("Запись не найдена.")
        return
    result = await _thread(
        warehouse_service.find_product,
        message.text or "",
        str(operation["operation_type"]),
        actor_user_id=message.from_user.id,
    )
    if result.exact_id is None:
        if result.candidates:
            await message.answer(
                "Выберите продукт:",
                reply_markup=edit_product_candidates(
                    int(operation["id"]), result.candidates
                ),
            )
        else:
            await message.answer("Продукт не найден. Введите название ещё раз.")
        return
    await _prepare_product_change(
        message, state, warehouse_service, operation, result.exact_id,
        message.from_user.id,
    )


async def _prepare_product_change(
    message: Message,
    state: FSMContext,
    service: WarehouseOperationsService,
    operation: dict,
    product_id: int,
    user_id: int,
) -> None:
    product = await _thread(
        service.product_option, product_id, actor_user_id=user_id
    )
    if product is None:
        await message.answer("Продукт больше недоступен.")
        return
    if product.base_unit.value != operation["base_unit"]:
        await state.set_state(WarehouseOperationEditStates.quantity)
        await state.update_data(pending_product_id=product.id)
        await message.answer(
            "Основная единица нового продукта отличается. "
            "Введите новое количество вместе с подходящей единицей."
        )
        return
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(edit_kind="product", product_id=product.id)
    await message.answer(
        f"Изменить продукт на «{escape(product.official_name)}»?",
        reply_markup=confirm_edit_keyboard(
            int(operation["id"]), int(operation["revision"])
        ),
    )


@router.callback_query(
    WarehouseCallback.filter(F.action == "pe"),
    WarehouseOperationEditStates.product,
)
async def edit_product_choice(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    if int(data.get("operation_id", 0)) != callback_data.object_id:
        return
    operation = await _thread(
        warehouse_service.operation_view,
        callback_data.object_id,
        actor_user_id=callback.from_user.id,
    )
    if operation is not None and isinstance(callback.message, Message):
        await _prepare_product_change(
            callback.message, state, warehouse_service, operation,
            callback_data.aux, callback.from_user.id,
        )


@router.message(WarehouseOperationEditStates.quantity, F.text)
async def edit_quantity_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    operation = await _thread(
        warehouse_service.operation_view,
        int(data["operation_id"]),
        actor_user_id=message.from_user.id,
    )
    if operation is None:
        return
    try:
        parsed = parse_intake_message(f"списание: продукт {message.text or ''}").lines[0]
        product_id = int(data.get("pending_product_id", operation["product_id"]))
        product = await _thread(
            warehouse_service.product_option,
            product_id,
            actor_user_id=message.from_user.id,
        )
        if product is None:
            raise WarehouseOperationsError("Product not found")
        quantity = parse_quantity(parsed.quantity_text, parsed.unit_text, product.base_unit)
    except (IntakeParseError, QuantityError, WarehouseOperationsError) as exc:
        await message.answer(f"Некорректное количество: {escape(str(exc))}")
        return
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(
        edit_kind="product_quantity" if data.get("pending_product_id") else "quantity",
        quantity_text=parsed.quantity_text,
        unit_text=parsed.unit_text,
        product_id=product_id,
    )
    await message.answer(
        f"Новое количество: {format_scaled(quantity.quantity_scaled, product.base_unit.value)}",
        reply_markup=confirm_edit_keyboard(
            int(operation["id"]), int(data["revision"])
        ),
    )


@router.message(WarehouseOperationEditStates.note, F.text)
async def edit_note_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    if len(text) > 500:
        await message.answer("Текст слишком длинный. Максимум 500 символов.")
        return
    data = await state.get_data()
    operation = await _thread(
        warehouse_service.operation_view,
        int(data["operation_id"]),
        actor_user_id=message.from_user.id,
    )
    if operation is None:
        return
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(edit_kind="note", note=None if text == "-" else text)
    await message.answer(
        "Очистить текст?" if text == "-" else f"Новый текст: {escape(text)}",
        reply_markup=confirm_edit_keyboard(
            int(operation["id"]), int(data["revision"])
        ),
    )


@router.message(WarehouseOperationEditStates.development_title, F.text)
async def edit_development_title_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    title = (message.text or "").strip()
    if not title or len(title) > 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return
    data = await state.get_data()
    operation = await _thread(
        warehouse_service.operation_view,
        int(data["operation_id"]),
        actor_user_id=message.from_user.id,
    )
    if operation is None or operation["operation_type"] != "development":
        await message.answer("Проработка не найдена.")
        return
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(edit_kind="development_title", development_title=title)
    await message.answer(
        "Переименовать всю проработку во всех её позициях?\n\n"
        f"Новое название: {escape(title)}",
        reply_markup=confirm_edit_keyboard(
            int(operation["id"]), int(operation["revision"])
        ),
    )


@router.message(WarehouseOperationEditStates.destination, F.text)
async def edit_destination_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    operation = await _thread(
        warehouse_service.operation_view,
        int(data["operation_id"]),
        actor_user_id=message.from_user.id,
    )
    if operation is None:
        return
    result = await _thread(
        warehouse_service.find_destination,
        message.text or "",
        source_location_id=int(operation["source_location_id"]),
        actor_user_id=message.from_user.id,
    )
    if result.exact_id is None:
        options = await _thread(
            warehouse_service.destination_options,
            actor_user_id=message.from_user.id,
        )
        await message.answer(
            "Место не определено. Выберите:",
            reply_markup=edit_destination_candidates(
                int(operation["id"]), options
            ),
        )
        return
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(edit_kind="destination", destination_id=result.exact_id)
    await message.answer(
        "Подтвердите новое место назначения.",
        reply_markup=confirm_edit_keyboard(
            int(operation["id"]), int(data["revision"])
        ),
    )


@router.callback_query(
    WarehouseCallback.filter(F.action == "de"),
    WarehouseOperationEditStates.destination,
)
async def edit_destination_choice(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if int(data.get("operation_id", 0)) != callback_data.object_id:
        await callback.answer("Устаревшая кнопка", show_alert=True)
        return
    await callback.answer()
    await state.set_state(WarehouseOperationEditStates.confirm)
    await state.update_data(edit_kind="destination", destination_id=callback_data.aux)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Подтвердите новое место назначения.",
            reply_markup=confirm_edit_keyboard(
                callback_data.object_id, int(data["revision"])
            ),
        )


@router.callback_query(
    WarehouseCallback.filter(F.action == "ec"),
    WarehouseOperationEditStates.confirm,
)
async def edit_confirm(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    if (
        int(data.get("operation_id", 0)) != callback_data.object_id
        or int(data.get("revision", 0)) != callback_data.aux
    ):
        await callback.answer("Устаревшее подтверждение", show_alert=True)
        return
    await callback.answer()
    try:
        kind = data["edit_kind"]
        common = {
            "expected_revision": callback_data.aux,
            "actor_user_id": callback.from_user.id,
        }
        if kind == "product":
            updated = await _thread(
                warehouse_service.update_product,
                callback_data.object_id,
                int(data["product_id"]),
                **common,
            )
        elif kind == "quantity":
            updated = await _thread(
                warehouse_service.update_quantity,
                callback_data.object_id,
                str(data["quantity_text"]),
                data.get("unit_text"),
                **common,
            )
        elif kind == "product_quantity":
            updated = await _thread(
                warehouse_service.update_product_and_quantity,
                callback_data.object_id,
                int(data["product_id"]),
                str(data["quantity_text"]),
                data.get("unit_text"),
                **common,
            )
        elif kind == "note":
            operation = await _thread(
                warehouse_service.operation_view,
                callback_data.object_id,
                actor_user_id=callback.from_user.id,
            )
            updated = await _thread(
                warehouse_service.update_note,
                callback_data.object_id,
                reason_text=data.get("note") if operation["operation_type"] == "writeoff" else None,
                comment_text=data.get("note") if operation["operation_type"] != "writeoff" else None,
                **common,
            )
        elif kind == "development_title":
            updated = await _thread(
                warehouse_service.rename_development,
                callback_data.object_id,
                str(data["development_title"]),
                **common,
            )
        else:
            updated = await _thread(
                warehouse_service.update_destination,
                callback_data.object_id,
                int(data["destination_id"]),
                **common,
            )
    except WarehouseRevisionConflict:
        return_page = int(data.get("warehouse_return_page", 0))
        await state.clear()
        await state.update_data(warehouse_return_page=return_page)
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(WarehouseRevisionConflict()))
            await _show_operation(
                callback.message, warehouse_service, auth_service,
                callback_data.object_id, callback.from_user.id,
                return_page=return_page,
            )
        return
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    return_page = int(data.get("warehouse_return_page", 0))
    await state.clear()
    await state.update_data(warehouse_return_page=return_page)
    if isinstance(callback.message, Message):
        await callback.message.answer("✅ Изменение сохранено.")
        await _show_operation(
            callback.message, warehouse_service, auth_service,
            int(updated["id"]), callback.from_user.id,
            return_page=return_page,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "dx"))
async def delete_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    context = await _thread(auth_service.get_context, callback.from_user.id)
    if not context.can_mutate_warehouse:
        await callback.answer("Доступ только для чтения", show_alert=True)
        return
    operation = await _thread(
        warehouse_service.operation_view,
        callback_data.object_id,
        actor_user_id=callback.from_user.id,
    )
    if (
        operation is None
        or int(operation["revision"]) != callback_data.aux
        or operation["is_deleted"]
    ):
        await callback.answer("Устаревшая кнопка", show_alert=True)
        return
    await callback.answer()
    await state.set_state(WarehouseOperationEditStates.delete)
    await state.update_data(
        operation_id=callback_data.object_id, revision=callback_data.aux
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Удалить запись?\n\n"
            f"{escape(str(operation['product_name_snapshot']))} — "
            f"{format_scaled(int(operation['quantity_scaled']), str(operation['base_unit']))}",
            reply_markup=delete_keyboard(
                callback_data.object_id, callback_data.aux
            ),
        )


@router.callback_query(
    WarehouseCallback.filter(F.action == "dc"),
    WarehouseOperationEditStates.delete,
)
async def delete_confirm(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    if (
        int(data.get("operation_id", 0)) != callback_data.object_id
        or int(data.get("revision", 0)) != callback_data.aux
    ):
        await callback.answer("Устаревшее подтверждение", show_alert=True)
        return
    await callback.answer()
    try:
        await _thread(
            warehouse_service.delete_operation,
            callback_data.object_id,
            expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
            deletion_reason="Удалено через Telegram",
        )
    except Exception as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("✅ Запись удалена.")


@router.message(StateFilter(None), F.text, WarehouseTriggerFilter())
async def direct_trigger(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    context = await _thread(auth_service.get_context, message.from_user.id)
    if context.role is Role.ACCOUNTANT:
        await message.answer("Для вашей роли склад доступен только для чтения.")
        return
    if not context.can_mutate_warehouse:
        return
    if not await _configured(message, warehouse_service, context.role):
        return
    await _prepare(message, state, warehouse_service, message.text or "")
