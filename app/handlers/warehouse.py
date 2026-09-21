"""Telegram UI for warehouse write-offs, transfers, and current-shift records."""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape

from aiogram import Bot, F, Router
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
    line_comment_choice_keyboard,
    line_comment_confirm_keyboard,
    nomenclature_confirm_keyboard,
    nomenclature_request_keyboard,
    nomenclature_unit_keyboard,
    operation_keyboard,
    operation_list_keyboard,
    problem_line_keyboard,
    records_keyboard,
    recovery_keyboard,
    warehouse_home_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.models.user import Role
from app.models.warehouse import (
    BaseUnit,
    ProductOperationType,
    WarehouseProduct,
    WarehouseProductCreate,
)
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
from app.services.warehouse.nomenclature_service import WarehouseNomenclatureService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.quantity_service import (
    QuantityError,
    compatible_unit_choices,
    parse_quantity,
)
from app.storage.warehouse_database import WarehouseNameConflictError
from app.storage.warehouse_operations_database import (
    WarehouseOperationsError,
    WarehouseRevisionConflict,
)
from app.utils.warehouse import normalize_warehouse_name

router = Router(name=__name__)
logger = logging.getLogger(__name__)

WAREHOUSE_TITLE = "📦 <b>Складские операции</b>"
WRITEOFF_PROMPT = (
    "Отправьте одну или несколько позиций, каждую с новой строки.\n\n"
    "Пример:\nапельсин 0.15 кг\nморковь 300 г (сгнила)\n\n"
    "Указывайте единицу явно. Если её пропустить, бот попросит выбрать "
    "единицу перед сохранением."
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

_BASE_UNITS = (BaseUnit.KG, BaseUnit.L, BaseUnit.PCS)
_NUMBER_TOKEN_RE = re.compile(
    r"(?:^|\s)[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:\s|$)"
)
_UNIT_TOKEN_RE = re.compile(
    r"(?:^|\s)(?:кг|kg|г|гр|g|л|l|мл|ml|шт\.?|pcs)(?:\s|$)",
    re.IGNORECASE,
)


def _canonical_product_name(
    value: str,
    location_names: tuple[str, ...] = (),
) -> str:
    """Reject intake syntax so only a product name reaches nomenclature."""

    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200:
        raise ValueError("Название должно содержать от 1 до 200 символов.")
    if "->" in cleaned or "(" in cleaned or ")" in cleaned:
        raise ValueError(
            "Введите только название — без стрелки, направления и комментария."
        )
    if _NUMBER_TOKEN_RE.search(cleaned) or _UNIT_TOKEN_RE.search(cleaned):
        raise ValueError(
            "Введите только название — без количества и единицы измерения."
        )
    normalized = normalize_warehouse_name(cleaned)
    for location_name in location_names:
        location = normalize_warehouse_name(location_name)
        variants = {location}
        if location.endswith("а"):
            variants.add(location[:-1] + "у")
        elif location.endswith("я"):
            variants.add(location[:-1] + "ю")
        elif location and location[-1].isalpha():
            variants.add(location + "а")
        if any(
            normalized == variant
            or normalized.endswith((f" из {variant}", f" в {variant}", f" на {variant}"))
            for variant in variants
        ):
            raise ValueError(
                "Введите только название — без источника или направления."
            )
    return cleaned


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
    if "подтвердите комментарий" in value:
        return "Сначала подтвердите комментарий для каждой позиции."
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
            can_manage_delivery_schedules=context.can_manage_warehouse_delivery_schedules,
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
        if (
            batch["operation_type"] in {"writeoff", "transfer"}
            and await _show_next_comment_line(message, state, batch)
        ):
            return
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
    product_text = str(problem.get("product_text") or "").strip()
    product_name = str(problem.get("product_name") or product_text).strip()
    error_code = str(problem.get("error_code") or "")
    candidates = problem.get("candidates") or []
    if error_code in {"unknown_product", "ambiguous_product"} and product_text:
        await message.answer(
            f"Продукт “{escape(product_text)}” не найден в номенклатуре.\n\n"
            + ("Возможно, вы имели в виду:" if candidates else ""),
            reply_markup=problem_line_keyboard(
                int(batch["id"]), int(problem["id"]), candidates,
                unknown_product_text=product_text,
                product_only=True,
            ),
        )
        return
    if error_code in {
        "missing_quantity", "invalid_quantity", "non_positive_quantity",
        "excess_precision", "quantity_overflow", "unsupported_unit",
        "incompatible_unit",
    } and problem.get("product_id"):
        await state.set_state(WarehouseIntakeStates.quantity)
        await state.update_data(
            batch_id=int(batch["id"]), current_line_id=int(problem["id"])
        )
        lead = (
            f"Для “{escape(product_name)}” не указано количество."
            if error_code == "missing_quantity"
            else f"Для “{escape(product_name)}” указано некорректное количество или единица."
        )
        await message.answer(
            f"{lead}\n\nВведите количество, например: 0,5; 0.5; 500 мл; 0,5 л. "
            "Если единицу не указать, будет использована базовая единица продукта."
        )
        return
    if error_code == "missing_unit":
        product = await _thread(
            service.product_option,
            int(problem["product_id"]),
            actor_user_id=user_id,
        )
        if product is not None:
            lead = (
                f"Для “{escape(product_name)}” не указана единица измерения."
                if error_code == "missing_unit"
                else f"Для “{escape(product_name)}” указана неизвестная или несовместимая единица."
            )
            await message.answer(
                f"{lead} Выберите единицу:",
                reply_markup=problem_line_keyboard(
                    int(batch["id"]), int(problem["id"]), [],
                    units=compatible_unit_choices(product.base_unit),
                ),
            )
            return
    if error_code in {
        "missing_destination", "unknown_destination", "ambiguous_destination",
        "same_source_destination",
    } and problem.get("product_id"):
        destinations = await _thread(service.destination_options, actor_user_id=user_id)
        destination_text = escape(str(problem.get("destination_text") or ""))
        if error_code == "missing_destination":
            quantity = format_scaled(
                int(problem["quantity_scaled"]), str(problem["base_unit"])
            )
            prompt = f"Куда переместить «{escape(product_name)}» — {quantity}?"
        else:
            destination_error = {
                "unknown_destination": (
                    f"Направление “{destination_text}” не найдено."
                ),
                "ambiguous_destination": (
                    f"Не удалось точно определить направление “{destination_text}”."
                ),
                "same_source_destination": (
                    "Источник и направление не могут совпадать."
                ),
            }[error_code]
            prompt = f"{destination_error}\n\nВыберите направление:"
        await message.answer(
            prompt,
            reply_markup=problem_line_keyboard(
                int(batch["id"]),
                int(problem["id"]),
                [],
                destinations=destinations,
            ),
        )
        return
    reasons = {
        "invalid_line": "неверное количество",
        "invalid_transfer_arrow": "нужна ровно одна стрелка -> и место назначения",
        "invalid_destination": "не указано корректное место назначения",
        "header_destination_conflict": (
            "место уже указано в заголовке — уберите стрелку из строки"
        ),
        "malformed_parentheses": "комментарий должен быть в одной паре скобок в конце",
        "incompatible_unit": "единица несовместима с основной единицей продукта",
        "unsupported_unit": "единица измерения не поддерживается",
        "invalid_quantity": "не найдено корректное количество",
        "non_positive_quantity": "количество должно быть больше нуля",
        "excess_precision": "у количества слишком высокая точность",
        "quantity_overflow": "количество превышает допустимый предел",
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


def _line_note(batch: dict, line: dict) -> str | None:
    value = (
        line.get("reason_text")
        if batch["operation_type"] == "writeoff"
        else line.get("comment_text")
    )
    return str(value) if value else None


def _line_confirmation_text(batch: dict, line: dict) -> str:
    name = escape(str(line.get("product_name") or line.get("product_text") or "—"))
    quantity = format_scaled(int(line["quantity_scaled"]), str(line["base_unit"]))
    kind = "списание" if batch["operation_type"] == "writeoff" else "перемещение"
    lines = [
        "<b>Позиция распознана:</b>",
        f"{name} — {quantity}",
        f"Операция: {kind}",
    ]
    if batch["operation_type"] == "transfer":
        destination = escape(
            str(line.get("destination_name") or line.get("destination_text") or "—")
        )
        lines.extend(("Из: Бар", f"В: {destination}"))
    note = _line_note(batch, line)
    lines.append(
        f"Комментарий: {escape(note) if note else 'без комментария'}"
    )
    return "\n".join(lines)


async def _show_line_confirmation(
    message: Message,
    state: FSMContext,
    batch: dict,
    line: dict,
) -> None:
    await state.set_state(WarehouseIntakeStates.comment_confirm)
    await state.update_data(
        batch_id=int(batch["id"]),
        current_line_id=int(line["id"]),
    )
    await message.answer(
        _line_confirmation_text(batch, line),
        reply_markup=line_comment_confirm_keyboard(
            int(batch["id"]),
            int(line["id"]),
            has_comment=_line_note(batch, line) is not None,
        ),
    )


async def _show_next_comment_line(
    message: Message,
    state: FSMContext,
    batch: dict,
) -> bool:
    data = await state.get_data()
    same_batch = int(data.get("batch_id", 0)) == int(batch["id"])
    confirmed = {
        int(value)
        for value in data.get("comment_confirmed_line_ids", [])
    } if same_batch else set()
    valid_ids = {int(line["id"]) for line in batch["lines"]}
    confirmed &= valid_ids
    line = next(
        (item for item in batch["lines"] if int(item["id"]) not in confirmed),
        None,
    )
    await state.update_data(
        batch_id=int(batch["id"]),
        comment_confirmed_line_ids=sorted(confirmed),
    )
    if line is None:
        return False
    if _line_note(batch, line) is not None:
        await _show_line_confirmation(message, state, batch, line)
        return True
    await state.set_state(WarehouseIntakeStates.comment_choice)
    await state.update_data(current_line_id=int(line["id"]))
    name = escape(str(line.get("product_name") or line.get("product_text") or "—"))
    quantity = format_scaled(int(line["quantity_scaled"]), str(line["base_unit"]))
    kind = "списание" if batch["operation_type"] == "writeoff" else "перемещение"
    direction = ""
    if batch["operation_type"] == "transfer":
        destination = escape(
            str(line.get("destination_name") or line.get("destination_text") or "—")
        )
        direction = f"\nИз: Бар\nВ: {destination}"
    await message.answer(
        "<b>Позиция распознана:</b>\n"
        f"{name} — {quantity}\n"
        f"Операция: {kind}{direction}\n\n"
        "Хотите добавить комментарий?",
        reply_markup=line_comment_choice_keyboard(
            int(batch["id"]), int(line["id"])
        ),
    )
    return True


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


@router.callback_query(WarehouseCallback.filter(F.action == "ru"))
async def batch_resolve_unit(
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
            warehouse_service.owned_batch,
            batch_id,
            actor_user_id=callback.from_user.id,
        )
        line = next(
            item for item in batch["lines"]
            if int(item["id"]) == callback_data.object_id
        )
        product = await _thread(
            warehouse_service.product_option,
            int(line["product_id"]),
            actor_user_id=callback.from_user.id,
        )
        if product is None or callback_data.aux not in range(
            1, len(compatible_unit_choices(product.base_unit)) + 1
        ):
            raise WarehouseOperationsError("Unit choice is not available")
        choices = compatible_unit_choices(product.base_unit)
        unit_text = choices[callback_data.aux - 1][0]
        batch = await _thread(
            warehouse_service.resolve_unit,
            batch_id,
            callback_data.object_id,
            unit_text,
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


@router.callback_query(WarehouseCallback.filter(F.action == "np"))
async def unknown_product_add_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    batch_id = int(data.get("batch_id", 0))
    try:
        batch = await _thread(
            warehouse_service.owned_batch,
            batch_id,
            actor_user_id=callback.from_user.id,
        )
        line = next(
            item for item in batch["lines"]
            if int(item["id"]) == callback_data.object_id
        )
        product_text = str(line.get("product_text") or "").strip()
        if not product_text or line.get("product_id") is not None:
            raise WarehouseOperationsError("Unknown product line not found")
    except (AccessDeniedError, StopIteration, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.update_data(
        batch_id=batch_id,
        current_line_id=callback_data.object_id,
        unknown_product_text=product_text,
        unknown_product_raw_line=str(line["raw_line"]),
        unknown_product_operation_type=str(batch["operation_type"]),
    )
    context = auth_service.get_context(callback.from_user.id)
    if not context.can_manage_warehouse_nomenclature:
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "У вас нет прав на изменение номенклатуры. "
                "Можно отправить запрос администратору.",
                reply_markup=nomenclature_request_keyboard(callback_data.object_id),
            )
        return
    await state.set_state(WarehouseIntakeStates.new_product_name)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Введите каноническое название продукта.\n\n"
            f"Распознано: “{escape(product_text)}”\n\n"
            "Укажите только название, без количества, единицы, направления и комментария."
        )


@router.message(WarehouseIntakeStates.new_product_name, F.text)
async def unknown_product_name_input(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    try:
        if message.from_user is None:
            return
        locations = await _thread(
            warehouse_service.location_options,
            actor_user_id=message.from_user.id,
        )
        name = _canonical_product_name(
            message.text or "",
            tuple(location.name for location in locations),
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    await state.update_data(new_product_name=name)
    await state.set_state(WarehouseIntakeStates.new_product_unit)
    await message.answer(
        f"Выберите базовую единицу для “{escape(name)}”:",
        reply_markup=nomenclature_unit_keyboard(int(data["current_line_id"])),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "nu"))
async def unknown_product_unit_choice(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    if (
        int(data.get("current_line_id", 0)) != callback_data.object_id
        or callback_data.aux not in range(1, len(_BASE_UNITS) + 1)
        or not data.get("new_product_name")
    ):
        if isinstance(callback.message, Message):
            await callback.message.answer("Сценарий добавления устарел. Начните заново.")
        return
    base_unit = _BASE_UNITS[callback_data.aux - 1]
    await state.update_data(new_product_base_unit=base_unit.value)
    await state.set_state(WarehouseIntakeStates.new_product_confirm)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Проверьте новый продукт:\n\n"
            f"Название: <b>{escape(str(data['new_product_name']))}</b>\n"
            f"Базовая единица: <b>{base_unit.value}</b>\n\n"
            "Продукт будет создан только после подтверждения.",
            reply_markup=nomenclature_confirm_keyboard(callback_data.object_id),
        )


def _operation_product_type(value: str) -> ProductOperationType:
    return {
        WarehouseOperationType.WRITEOFF.value: ProductOperationType.PRODUCT_WRITEOFF,
        WarehouseOperationType.TRANSFER.value: ProductOperationType.TRANSFER,
        WarehouseOperationType.DEVELOPMENT.value: ProductOperationType.DEVELOPMENT,
    }[value]


def _create_or_reuse_intake_product(
    service: WarehouseNomenclatureService,
    actor_user_id: int,
    command: WarehouseProductCreate,
) -> WarehouseProduct:
    """Create once, or reuse the same active product after a repeated callback."""

    try:
        return service.create_product(actor_user_id, command)
    except WarehouseNameConflictError:
        normalized = normalize_warehouse_name(command.official_name)
        return next(
            product
            for product in service.list_products(
                actor_user_id, include_inactive=False
            )
            if product.normalized_name == normalized
            and product.base_unit == command.base_unit
            and command.operation_types.issubset(product.operation_types)
        )


@router.callback_query(WarehouseCallback.filter(F.action == "nc"))
async def unknown_product_create_confirm(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
    warehouse_nomenclature_service: WarehouseNomenclatureService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    if int(data.get("current_line_id", 0)) != callback_data.object_id:
        if isinstance(callback.message, Message):
            await callback.message.answer("Сценарий добавления устарел. Начните заново.")
        return
    try:
        locations = await _thread(
            warehouse_service.location_options,
            actor_user_id=callback.from_user.id,
        )
        name = _canonical_product_name(
            str(data["new_product_name"]),
            tuple(location.name for location in locations),
        )
        base_unit = BaseUnit(str(data["new_product_base_unit"]))
        operation_type = _operation_product_type(
            str(data["unknown_product_operation_type"])
        )
        command = WarehouseProductCreate(
            official_name=name,
            base_unit=base_unit,
            aliases=[],
            operation_types={operation_type},
        )
        product = await _thread(
            _create_or_reuse_intake_product,
            warehouse_nomenclature_service,
            callback.from_user.id,
            command,
        )
        batch = await _thread(
            warehouse_service.resolve_product,
            int(data["batch_id"]),
            callback_data.object_id,
            product.id,
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, KeyError, StopIteration, ValueError, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.set_state(WarehouseIntakeStates.resolving)
    if isinstance(callback.message, Message):
        await callback.message.answer(f"Продукт “{escape(product.official_name)}” создан.")
        await _show_batch(
            callback.message, state, warehouse_service, batch, callback.from_user.id
        )


@router.callback_query(WarehouseCallback.filter(F.action == "nr"))
async def unknown_product_request_admin(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    bot: Bot,
    auth_service: AuthService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    if int(data.get("current_line_id", 0)) != callback_data.object_id:
        return
    recipients = set(auth_service.super_admin_ids)
    if auth_service.user_service is not None:
        admins, _, _ = await _thread(
            auth_service.user_service.list_users,
            role=Role.ADMIN,
            page_size=100_000,
        )
        recipients.update(
            user.telegram_user_id
            for user in admins
            if user.is_active and user.has_started
        )
    recipients.discard(callback.from_user.id)
    sent = 0
    request_text = (
        "📨 <b>Запрос на добавление складского продукта</b>\n\n"
        f"Продукт: “{escape(str(data.get('unknown_product_text') or ''))}”\n"
        f"Исходная строка: “{escape(str(data.get('unknown_product_raw_line') or ''))}”"
    )
    for recipient in recipients:
        try:
            await bot.send_message(recipient, request_text)
            sent += 1
        except Exception:
            logger.warning("Warehouse nomenclature request delivery failed")
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Запрос отправлен администратору. Исходная строка сохранена."
            if sent
            else "Не удалось доставить запрос администратору. Исходная строка сохранена."
        )


@router.callback_query(WarehouseCallback.filter(F.action == "nx"))
async def unknown_product_add_cancel(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    if int(data.get("current_line_id", 0)) != callback_data.object_id:
        if isinstance(callback.message, Message):
            await callback.message.answer("Сценарий добавления устарел. Начните заново.")
        return
    try:
        batch = await _thread(
            warehouse_service.owned_batch,
            int(data.get("batch_id", 0)),
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, WarehouseOperationsError):
        batch = None
    await state.set_state(WarehouseIntakeStates.resolving)
    if isinstance(callback.message, Message) and batch is not None:
        await callback.message.answer("Добавление продукта отменено.")
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


async def _comment_batch_line(
    state: FSMContext,
    service: WarehouseOperationsService,
    *,
    actor_user_id: int,
    line_id: int,
    expected_states: set[str],
) -> tuple[dict, dict]:
    current_state = await state.get_state()
    data = await state.get_data()
    if (
        current_state not in expected_states
        or int(data.get("current_line_id", 0)) != line_id
    ):
        raise WarehouseOperationsError("Comment step is no longer active")
    batch = await _thread(
        service.owned_batch,
        int(data.get("batch_id", 0)),
        actor_user_id=actor_user_id,
    )
    if batch is None or batch["operation_type"] not in {"writeoff", "transfer"}:
        raise WarehouseOperationsError("Comment batch is not available")
    line = next(
        (item for item in batch["lines"] if int(item["id"]) == line_id),
        None,
    )
    if line is None or line["status"] != "resolved":
        raise WarehouseOperationsError("Comment line is not available")
    return batch, line


@router.callback_query(WarehouseCallback.filter(F.action == "ja"))
@router.callback_query(WarehouseCallback.filter(F.action == "jm"))
async def line_comment_input_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    expected = {
        WarehouseIntakeStates.comment_choice.state
        if callback_data.action == "ja"
        else WarehouseIntakeStates.comment_confirm.state
    }
    try:
        await _comment_batch_line(
            state,
            warehouse_service,
            actor_user_id=callback.from_user.id,
            line_id=callback_data.object_id,
            expected_states=expected,
        )
    except (AccessDeniedError, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    await state.set_state(WarehouseIntakeStates.comment_text)
    await state.update_data(comment_mode=callback_data.action)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Напишите, что произошло с продуктом.\n"
            "Например: испортился, упал, использован для проработки"
        )


@router.message(WarehouseIntakeStates.comment_text, F.text)
async def line_comment_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    value = message.text or ""
    if value.lstrip().startswith("/"):
        await message.answer("Команда не может быть комментарием. Введите обычный текст.")
        return
    cleaned = " ".join(value.split())
    if not cleaned:
        await message.answer("Комментарий не может быть пустым.")
        return
    if len(cleaned) > 500:
        await message.answer("Комментарий слишком длинный. Максимум — 500 символов.")
        return
    data = await state.get_data()
    line_id = int(data.get("current_line_id", 0))
    try:
        batch, _ = await _comment_batch_line(
            state,
            warehouse_service,
            actor_user_id=message.from_user.id,
            line_id=line_id,
            expected_states={WarehouseIntakeStates.comment_text.state},
        )
        batch = await _thread(
            warehouse_service.update_intake_line_comment,
            int(batch["id"]),
            line_id,
            cleaned,
            actor_user_id=message.from_user.id,
        )
    except (AccessDeniedError, WarehouseIntakeError, WarehouseOperationsError) as exc:
        await message.answer(_error_text(exc))
        return
    line = next(item for item in batch["lines"] if int(item["id"]) == line_id)
    await _show_line_confirmation(message, state, batch, line)


@router.callback_query(WarehouseCallback.filter(F.action == "js"))
async def line_comment_skip(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        batch, _ = await _comment_batch_line(
            state,
            warehouse_service,
            actor_user_id=callback.from_user.id,
            line_id=callback_data.object_id,
            expected_states={WarehouseIntakeStates.comment_choice.state},
        )
        batch = await _thread(
            warehouse_service.update_intake_line_comment,
            int(batch["id"]),
            callback_data.object_id,
            None,
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, WarehouseIntakeError, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        line = next(
            item for item in batch["lines"]
            if int(item["id"]) == callback_data.object_id
        )
        await _show_line_confirmation(callback.message, state, batch, line)


@router.callback_query(WarehouseCallback.filter(F.action == "jd"))
async def line_comment_delete(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        batch, line = await _comment_batch_line(
            state,
            warehouse_service,
            actor_user_id=callback.from_user.id,
            line_id=callback_data.object_id,
            expected_states={WarehouseIntakeStates.comment_confirm.state},
        )
        if _line_note(batch, line) is None:
            raise WarehouseOperationsError("Comment is already absent")
        batch = await _thread(
            warehouse_service.update_intake_line_comment,
            int(batch["id"]),
            callback_data.object_id,
            None,
            actor_user_id=callback.from_user.id,
        )
    except (AccessDeniedError, WarehouseIntakeError, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    if isinstance(callback.message, Message):
        line = next(
            item for item in batch["lines"]
            if int(item["id"]) == callback_data.object_id
        )
        await _show_line_confirmation(callback.message, state, batch, line)


@router.callback_query(WarehouseCallback.filter(F.action == "jc"))
async def line_comment_confirm(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    try:
        batch, _ = await _comment_batch_line(
            state,
            warehouse_service,
            actor_user_id=callback.from_user.id,
            line_id=callback_data.object_id,
            expected_states={WarehouseIntakeStates.comment_confirm.state},
        )
    except (AccessDeniedError, WarehouseOperationsError) as exc:
        if isinstance(callback.message, Message):
            await callback.message.answer(_error_text(exc))
        return
    data = await state.get_data()
    confirmed = {
        int(value) for value in data.get("comment_confirmed_line_ids", [])
    }
    confirmed.add(callback_data.object_id)
    await state.update_data(comment_confirmed_line_ids=sorted(confirmed))
    if isinstance(callback.message, Message):
        await _show_batch(
            callback.message,
            state,
            warehouse_service,
            batch,
            callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "cl"))
async def line_correction_start(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    await callback.answer()
    data = await state.get_data()
    confirmed = {
        int(value) for value in data.get("comment_confirmed_line_ids", [])
    }
    confirmed.discard(callback_data.object_id)
    try:
        batch = await _thread(
            warehouse_service.owned_batch,
            int(data.get("batch_id", 0)),
            actor_user_id=callback.from_user.id,
        )
        line = next(
            item for item in batch["lines"]
            if int(item["id"]) == callback_data.object_id
        )
    except (AccessDeniedError, StopIteration, WarehouseOperationsError):
        if isinstance(callback.message, Message):
            await callback.message.answer("Незавершённая строка недоступна.")
        return
    product_only = line.get("error_code") in {
        "unknown_product", "ambiguous_product"
    }
    await state.set_state(
        WarehouseIntakeStates.correcting_product
        if product_only else WarehouseIntakeStates.correcting_line
    )
    await state.update_data(
        batch_id=int(data.get("batch_id", 0)),
        current_line_id=callback_data.object_id,
        comment_confirmed_line_ids=sorted(confirmed),
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите другое название продукта. Количество и остальные поля сохранятся."
            if product_only else "Отправьте исправленную строку целиком."
        )


@router.message(WarehouseIntakeStates.correcting_product, F.text)
async def line_product_correction_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Команда не может быть названием продукта.")
        return
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.replace_line_product_text,
            int(data["batch_id"]),
            int(data["current_line_id"]),
            text,
            actor_user_id=message.from_user.id,
        )
    except (WarehouseIntakeError, WarehouseOperationsError) as exc:
        await message.answer(_error_text(exc))
        return
    await _show_batch(message, state, warehouse_service, batch, message.from_user.id)


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


@router.message(WarehouseIntakeStates.quantity, F.text)
async def line_quantity_text(
    message: Message,
    state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Команда не может быть количеством.")
        return
    data = await state.get_data()
    try:
        batch = await _thread(
            warehouse_service.resolve_quantity_input,
            int(data["batch_id"]),
            int(data["current_line_id"]),
            text,
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
        batch = await _thread(
            warehouse_service.owned_batch,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if batch is None:
            raise WarehouseOperationsError("Batch is not available")
        if batch["operation_type"] in {"writeoff", "transfer"}:
            data = await state.get_data()
            confirmed = {
                int(value)
                for value in data.get("comment_confirmed_line_ids", [])
            }
            line_ids = {int(line["id"]) for line in batch["lines"]}
            if (
                int(data.get("batch_id", 0)) != int(batch["id"])
                or not line_ids
                or not line_ids <= confirmed
            ):
                raise WarehouseOperationsError(
                    "Сначала подтвердите комментарий для каждой позиции"
                )
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
    if operation is None or operation["operation_type"] != "transfer":
        await callback.answer("Перемещение не найдено", show_alert=True)
        return
    options = await _thread(
        warehouse_service.destination_options,
        actor_user_id=callback.from_user.id,
    )
    await callback.answer()
    await state.set_state(WarehouseOperationEditStates.destination)
    await state.update_data(
        operation_id=callback_data.object_id,
        revision=callback_data.aux,
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Выберите новое место назначения:",
            reply_markup=edit_destination_candidates(callback_data.object_id, options),
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
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    if int(data.get("operation_id", 0)) != callback_data.object_id:
        await callback.answer("Устаревшая кнопка", show_alert=True)
        return
    try:
        operation = await _thread(
            warehouse_service.operation_view,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        options = await _thread(
            warehouse_service.destination_options,
            actor_user_id=callback.from_user.id,
        )
        if operation is None or callback_data.aux not in {
            location.id for location in options
        }:
            raise WarehouseOperationsError("Destination not found")
    except (AccessDeniedError, WarehouseOperationsError) as exc:
        await callback.answer(_error_text(exc), show_alert=True)
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
