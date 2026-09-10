"""Telegram checklist and admin UI for required warehouse write-offs."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import (
    WarehouseClosingStates,
    WarehouseRequiredRuleStates,
)
from app.keyboards.warehouse import (
    closing_final_keyboard,
    closing_not_applicable_keyboard,
    closing_quantity_confirm_keyboard,
    closing_requirement_keyboard,
    closing_summary_keyboard,
    required_rule_keyboard,
    required_rules_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.models.warehouse.closing import RequiredQuantityMode, RequiredRuleInput
from app.models.warehouse.nomenclature import BaseUnit
from app.services.access_service import AccessDeniedError
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.formatting import (
    format_business_date,
    format_scaled,
)
from app.services.warehouse.quantity_service import QuantityError, parse_quantity
from app.storage.warehouse_closing_database import (
    WarehouseClosingConflict,
    WarehouseClosingError,
)

router = Router(name=__name__)
logger = logging.getLogger(__name__)

_NA_REASONS = {
    "n1": "Позиция не использовалась",
    "n2": "Остатков не было",
    "n3": "Уже списано ранее",
}
_WEEKDAY_BITS = {
    "пн": 1,
    "вт": 2,
    "ср": 4,
    "чт": 8,
    "пт": 16,
    "сб": 32,
    "вс": 64,
}


@router.message(Command("cancel"), StateFilter(WarehouseClosingStates))
async def closing_cancel_command(
    message: Message,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    session_id = data.get("closing_session_id")
    if session_id is not None:
        try:
            await _thread(
                warehouse_closing_service.cancel,
                int(session_id),
                actor_user_id=message.from_user.id,
            )
        except Exception as exc:
            await message.answer(_safe_error(exc))
            return
    await state.clear()
    await message.answer("Закрытие отменено.")


@router.message(Command("cancel"), StateFilter(WarehouseRequiredRuleStates))
async def required_rule_cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Редактирование правила отменено.")


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def _safe_error(error: Exception) -> str:
    if isinstance(error, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(error, WarehouseClosingConflict):
        return "Экран устарел: данные уже изменены. Откройте актуальную версию."
    if isinstance(error, QuantityError):
        return escape(str(error))
    if "No matching operation" in str(error):
        return (
            "Подходящего списания не найдено. Выберите другую причину "
            "или вернитесь к списку операций."
        )
    if isinstance(error, (WarehouseClosingError, ValidationError)):
        return escape(str(error))
    return "Временная ошибка склада. Попробуйте ещё раз."


def _pending(session: dict) -> dict | None:
    current_id = session.get("current_requirement_id")
    return next(
        (
            row
            for row in session["requirements"]
            if row["id"] == current_id and row["status"] == "pending"
        ),
        None,
    )


async def _show_session(
    message: Message,
    state: FSMContext,
    service: WarehouseClosingService,
    session: dict,
    user_id: int,
) -> None:
    if session.get("shift_status") == "closing" or session["status"] == "completed":
        await state.clear()
        await edit_or_send_text(
            message,
            "✅ Смена проверена и подготовлена к формированию отчёта.",
        )
        return
    requirement = _pending(session)
    if requirement is None:
        await state.clear()
        overview = await _thread(service.overview, actor_user_id=user_id)
        await edit_or_send_text(
            message,
            "✅ <b>Обязательные позиции заполнены</b>\n\n"
            f"Списание: {overview['writeoffs']} записей\n"
            f"Перемещение: {overview['transfers']} записей\n\n"
            "Подготовить смену к формированию отчёта?",
            closing_final_keyboard(int(session["id"]), int(session["revision"])),
        )
        return
    await state.clear()
    requirements = session["requirements"]
    number = next(
        index for index, row in enumerate(requirements, start=1)
        if row["id"] == requirement["id"]
    )
    recommended = (
        format_scaled(
            int(requirement["suggested_quantity_scaled"]),
            str(requirement["base_unit"]),
        )
        if requirement["suggested_quantity_scaled"] is not None
        else None
    )
    suffix = (
        f"\n\nРекомендуемое списание: {recommended}"
        if recommended is not None
        else "\n\nВведите количество:"
    )
    await edit_or_send_text(
        message,
        f"⚠️ <b>Обязательная позиция {number} из {len(requirements)}</b>\n\n"
        f"{escape(str(requirement['question_text']))}{suffix}",
        closing_requirement_keyboard(
            int(session["id"]),
            int(requirement["id"]),
            suggested_text=recommended,
            allow_not_applicable=bool(requirement["allow_not_applicable"]),
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "cs"))
async def closing_summary(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        overview = await _thread(
            warehouse_closing_service.overview,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        shift = overview["shift"]
        if shift["status"] == "closing":
            await edit_or_send_text(
                callback.message,
                "✅ Смена проверена и подготовлена к формированию отчёта.",
            )
            return
        await edit_or_send_text(
            callback.message,
            f"Смена: {format_business_date(str(shift['business_date']))}\n"
            f"Списание: {overview['writeoffs']} записей\n"
            f"Перемещение: {overview['transfers']} записей\n"
            "Обязательные позиции: "
            f"{overview['requirements_resolved']} из "
            f"{overview['requirements_total']} заполнено",
            closing_summary_keyboard(),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "cc"))
async def closing_start(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        session = await _thread(
            warehouse_closing_service.start,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_session(
            callback.message,
            state,
            warehouse_closing_service,
            session,
            callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "cv"))
async def closing_resume(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        session = await _thread(
            warehouse_closing_service.resume,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_session(
            callback.message,
            state,
            warehouse_closing_service,
            session,
            callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "qr"))
async def closing_recommended(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        session = await _thread(
            warehouse_closing_service.recommended,
            callback_data.object_id,
            callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Списание сохранено")
    if isinstance(callback.message, Message):
        await _show_session(
            callback.message, state, warehouse_closing_service,
            session, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "qo"))
async def closing_quantity_prompt(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        session = await _thread(
            warehouse_closing_service.resume,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        requirement = warehouse_closing_service._requirement(
            session, callback_data.aux
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseClosingStates.quantity)
    await state.update_data(
        closing_session_id=callback_data.object_id,
        closing_requirement_id=callback_data.aux,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Введите количество для «{escape(str(requirement['product_name']))}».\n"
            "Можно указать основную или совместимую единицу, например 0.35 или 350 мл."
        )


@router.message(WarehouseClosingStates.quantity, F.text)
async def closing_quantity_input(
    message: Message,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    try:
        preview = await _thread(
            warehouse_closing_service.quantity_preview,
            int(data["closing_session_id"]),
            int(data["closing_requirement_id"]),
            message.text or "",
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await state.set_state(WarehouseClosingStates.quantity_confirm)
    await state.update_data(closing_preview=preview)
    await message.answer(
        f"{escape(str(preview['product_name']))} — "
        f"{format_scaled(int(preview['quantity_scaled']), str(preview['base_unit']))}\n"
        "Сохранить?",
        reply_markup=closing_quantity_confirm_keyboard(
            int(preview["session_id"]), int(preview["requirement_id"])
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "qs"))
async def closing_quantity_save(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    data = await state.get_data()
    preview = data.get("closing_preview")
    if (
        not isinstance(preview, dict)
        or preview.get("session_id") != callback_data.object_id
        or preview.get("requirement_id") != callback_data.aux
    ):
        await callback.answer("Предпросмотр устарел. Введите количество заново.", show_alert=True)
        return
    try:
        session = await _thread(
            warehouse_closing_service.save_quantity,
            preview,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Списание сохранено")
    if isinstance(callback.message, Message):
        await _show_session(
            callback.message, state, warehouse_closing_service,
            session, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "qn"))
async def closing_not_applicable(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.resume,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "Почему сегодня не выполнялось списание?",
            closing_not_applicable_keyboard(
                callback_data.object_id, callback_data.aux
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action.in_({"n1", "n2", "n3"})))
async def closing_not_applicable_choice(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        session = await _thread(
            warehouse_closing_service.mark_not_applicable,
            callback_data.object_id,
            callback_data.aux,
            _NA_REASONS[callback_data.action],
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Причина сохранена")
    if isinstance(callback.message, Message):
        await _show_session(
            callback.message, state, warehouse_closing_service,
            session, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "n4"))
async def closing_not_applicable_other(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.resume,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseClosingStates.not_applicable_reason)
    await state.update_data(
        closing_session_id=callback_data.object_id,
        closing_requirement_id=callback_data.aux,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите обязательную причину (до 500 символов).")


@router.message(WarehouseClosingStates.not_applicable_reason, F.text)
async def closing_not_applicable_text(
    message: Message,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    try:
        session = await _thread(
            warehouse_closing_service.mark_not_applicable,
            int(data["closing_session_id"]),
            int(data["closing_requirement_id"]),
            message.text or "",
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await _show_session(
        message, state, warehouse_closing_service, session, message.from_user.id
    )


@router.callback_query(WarehouseCallback.filter(F.action == "qx"))
async def closing_cancel(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.cancel,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Закрытие отменено")
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "Закрытие отменено. Уже сохранённые списания остались в смене.",
        )


@router.callback_query(WarehouseCallback.filter(F.action == "cf"))
async def closing_finalize(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.finalize,
            callback_data.object_id,
            expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "✅ Смена проверена и подготовлена к формированию отчёта.",
        )


def _rule_text(rule: dict) -> str:
    suggested = (
        format_scaled(int(rule["suggested_quantity_scaled"]), str(rule["base_unit"]))
        if rule["suggested_quantity_scaled"] is not None
        else "вручную"
    )
    minimum = (
        format_scaled(int(rule["minimum_scaled"]), str(rule["base_unit"]))
        if rule["minimum_scaled"] is not None else "—"
    )
    maximum = (
        format_scaled(int(rule["maximum_scaled"]), str(rule["base_unit"]))
        if rule["maximum_scaled"] is not None else "—"
    )
    return (
        f"<b>{escape(str(rule['product_name']))}</b>\n\n"
        f"Вопрос: {escape(str(rule['question_text']))}\n"
        f"Режим: {rule['quantity_mode']} ({suggested})\n"
        f"Дни (маска): {rule['weekdays_mask']}\n"
        f"Мин/макс: {minimum} / {maximum}\n"
        f"Порядок: {rule['sort_order']}\n"
        "Можно отметить неприменимым: "
        f"{'да' if rule['allow_not_applicable'] else 'нет'}\n"
        f"Статус: {'активно' if rule['is_active'] else 'неактивно'}"
    )


async def _show_rule(
    message: Message,
    service: WarehouseClosingService,
    rule_id: int,
    user_id: int,
) -> None:
    rule = await _thread(service.get_rule, rule_id, actor_user_id=user_id)
    if rule is None:
        await message.answer("Правило не найдено.")
        return
    await edit_or_send_text(message, _rule_text(rule), required_rule_keyboard(rule))


@router.callback_query(WarehouseCallback.filter(F.action == "rm"))
async def required_rules_list(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        rules = await _thread(
            warehouse_closing_service.list_rules,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "✅ <b>Обязательные списания</b>\n\n"
            "Изменения применяются только к новым сменам.",
            required_rules_keyboard(rules),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "rv"))
async def required_rule_view(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.get_rule,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_rule(
            callback.message, warehouse_closing_service,
            callback_data.object_id, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "rn"))
async def required_rule_create_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        await _thread(
            warehouse_closing_service.list_rules,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseRequiredRuleStates.create)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Отправьте правило одной строкой:\n"
            "<code>Название продукта | manual | Вопрос</code>\n"
            "или\n"
            "<code>Название продукта | recommended | 0.06 | Вопрос</code>"
        )


@router.message(WarehouseRequiredRuleStates.create, F.text)
async def required_rule_create_input(
    message: Message,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is None:
        return
    parts = [part.strip() for part in (message.text or "").split("|")]
    try:
        if len(parts) not in {3, 4}:
            raise WarehouseClosingError("Неверный формат правила")
        mode = RequiredQuantityMode(parts[1].casefold())
        if (mode is RequiredQuantityMode.MANUAL) != (len(parts) == 3):
            raise WarehouseClosingError("Неверный формат режима")
        result = await _thread(
            warehouse_closing_service.operations.find_product,
            parts[0],
            "writeoff",
            actor_user_id=message.from_user.id,
        )
        if result.exact_id is None:
            raise WarehouseClosingError("Продукт не найден точно")
        product = await _thread(
            warehouse_closing_service.operations.product_option,
            result.exact_id,
            actor_user_id=message.from_user.id,
        )
        assert product is not None
        suggested = None
        question = parts[2]
        if mode is RequiredQuantityMode.RECOMMENDED:
            suggested = parse_quantity(parts[2], None, product.base_unit).quantity_scaled
            question = parts[3]
        rule = await _thread(
            warehouse_closing_service.create_rule,
            RequiredRuleInput(
                product_id=product.id,
                quantity_mode=mode,
                suggested_quantity_scaled=suggested,
                base_unit=product.base_unit,
                question_text=question,
            ),
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await state.clear()
    await _show_rule(
        message, warehouse_closing_service, int(rule["id"]), message.from_user.id
    )


@router.callback_query(
    WarehouseCallback.filter(F.action.in_({"zq", "zm", "zw", "zl", "zo"}))
)
async def required_rule_edit_prompt(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        rule = await _thread(
            warehouse_closing_service.get_rule,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if rule is None or int(rule["revision"]) != callback_data.aux:
            raise WarehouseClosingConflict("stale rule")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    states = {
        "zq": WarehouseRequiredRuleStates.question,
        "zm": WarehouseRequiredRuleStates.mode,
        "zw": WarehouseRequiredRuleStates.weekdays,
        "zl": WarehouseRequiredRuleStates.limits,
        "zo": WarehouseRequiredRuleStates.sort_order,
    }
    prompts = {
        "zq": "Введите новый вопрос (до 500 символов).",
        "zm": "Введите <code>manual</code> или <code>recommended 0.06</code>.",
        "zw": "Введите <code>ежедневно</code> либо дни: <code>пн,ср,пт</code>.",
        "zl": "Введите минимум и максимум: <code>- -</code> или <code>0.1 2</code>.",
        "zo": "Введите целое число для порядка.",
    }
    await state.set_state(states[callback_data.action])
    await state.update_data(
        rule_id=callback_data.object_id,
        rule_revision=callback_data.aux,
        rule_base_unit=rule["base_unit"],
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(prompts[callback_data.action])


@router.message(
    StateFilter(
        WarehouseRequiredRuleStates.question,
        WarehouseRequiredRuleStates.mode,
        WarehouseRequiredRuleStates.weekdays,
        WarehouseRequiredRuleStates.limits,
        WarehouseRequiredRuleStates.sort_order,
    ),
    F.text,
)
async def required_rule_edit_input(
    message: Message,
    state: FSMContext,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    current_state = await state.get_state()
    raw = (message.text or "").strip()
    changes: dict[str, object]
    try:
        if current_state == WarehouseRequiredRuleStates.question.state:
            changes = {"question_text": raw}
        elif current_state == WarehouseRequiredRuleStates.mode.state:
            parts = raw.split()
            mode = RequiredQuantityMode(parts[0].casefold())
            suggested = None
            if mode is RequiredQuantityMode.RECOMMENDED:
                if len(parts) != 2:
                    raise WarehouseClosingError("Укажите рекомендацию")
                suggested = parse_quantity(
                    parts[1], None, BaseUnit(data["rule_base_unit"])
                ).quantity_scaled
            elif len(parts) != 1:
                raise WarehouseClosingError("Для manual количество не задаётся")
            changes = {
                "quantity_mode": mode,
                "suggested_quantity_scaled": suggested,
            }
        elif current_state == WarehouseRequiredRuleStates.weekdays.state:
            if raw.casefold() == "ежедневно":
                mask = 127
            else:
                values = [value.strip().casefold() for value in raw.split(",")]
                if not values or any(value not in _WEEKDAY_BITS for value in values):
                    raise WarehouseClosingError("Неизвестный день недели")
                mask = sum({_WEEKDAY_BITS[value] for value in values})
            changes = {"weekdays_mask": mask}
        elif current_state == WarehouseRequiredRuleStates.limits.state:
            parts = raw.split()
            if len(parts) != 2:
                raise WarehouseClosingError("Укажите два значения")
            values = [
                None if value == "-" else parse_quantity(
                    value, None, BaseUnit(data["rule_base_unit"])
                ).quantity_scaled
                for value in parts
            ]
            changes = {"minimum_scaled": values[0], "maximum_scaled": values[1]}
        else:
            changes = {"sort_order": int(raw)}
        rule = await _thread(
            warehouse_closing_service.patch_rule,
            int(data["rule_id"]),
            changes,
            expected_revision=int(data["rule_revision"]),
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await state.clear()
    await _show_rule(
        message, warehouse_closing_service, int(rule["id"]), message.from_user.id
    )


@router.callback_query(WarehouseCallback.filter(F.action.in_({"za", "zx"})))
async def required_rule_toggle(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_closing_service: WarehouseClosingService,
) -> None:
    try:
        current = await _thread(
            warehouse_closing_service.get_rule,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if current is None or int(current["revision"]) != callback_data.aux:
            raise WarehouseClosingConflict("stale rule")
        field = "allow_not_applicable" if callback_data.action == "za" else "is_active"
        rule = await _thread(
            warehouse_closing_service.patch_rule,
            callback_data.object_id,
            {field: not bool(current[field])},
            expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Сохранено")
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message, _rule_text(rule), required_rule_keyboard(rule)
        )
