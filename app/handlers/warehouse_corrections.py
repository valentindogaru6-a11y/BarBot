"""Telegram UI for editing operations in an active closed-shift correction."""

from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import WarehouseCorrectionStates
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.keyboards.warehouse_corrections import (
    correction_cancel_session_keyboard,
    correction_candidates_keyboard,
    correction_dashboard_keyboard,
    correction_destinations_keyboard,
    correction_delivery_confirm_keyboard,
    correction_finalize_keyboard,
    correction_mutation_confirm_keyboard,
    correction_open_confirm_keyboard,
    correction_operation_keyboard,
    correction_operations_keyboard,
    correction_recipient_keyboard,
    correction_recovery_keyboard,
    correction_result_keyboard,
    correction_retry_keyboard,
    correction_versions_keyboard,
)
from app.models.warehouse.operations import WarehouseOperationType
from app.services.access_service import AccessDeniedError
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.formatting import format_business_date, format_scaled
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.quantity_service import QuantityError, parse_quantity
from app.storage.warehouse_correction_database import (
    WarehouseCorrectionConflict,
    WarehouseCorrectionError,
)

router = Router(name=__name__)


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def _safe_error(error: Exception) -> str:
    if isinstance(error, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(error, WarehouseCorrectionConflict):
        return "Данные уже изменились. Открыта актуальная версия."
    if isinstance(error, QuantityError):
        return str(error)
    if isinstance(error, WarehouseCorrectionError):
        value = str(error).casefold()
        if "no mutations" in value:
            return "Сначала внесите хотя бы одно изменение."
        if "select at least one recipient" in value:
            return "Выберите хотя бы одного получателя."
        if "no active recipients" in value:
            return "Нет активных получателей."
        if "no active failed deliveries" in value:
            return "Неуспешных доставок активным получателям нет."
        if "mutations" in value:
            return "После первого изменения отмена исправления недоступна."
        if "editable" in value or "revision" in value:
            return "Исправление уже изменилось. Откройте актуальную версию."
        return "Исправление недоступно или его состояние изменилось."
    return "Не удалось выполнить действие. Обновите экран и попробуйте снова."


def _quantity_parts(value: str) -> tuple[str, str | None]:
    parts = value.strip().split(maxsplit=1)
    if not parts:
        raise QuantityError("invalid_quantity", "Укажите количество")
    return parts[0], parts[1] if len(parts) == 2 else None


def _status_label(value: str) -> str:
    return {
        "open": "активно", "building": "формируется", "failed": "ошибка",
        "completed": "завершено", "cancelled": "отменено",
    }.get(value, value)


def dashboard_text(session: dict) -> str:
    counts = session["change_counts"]
    requirements = session["requirements"]
    required_total = sum(requirements.values())
    required_pending = int(requirements.get("pending", 0))
    checklist = (
        "нет обязательных позиций"
        if not required_total else
        f"выполнено {required_total - required_pending} из {required_total}"
    )
    return (
        "✏️ <b>Исправление закрытой смены</b>\n\n"
        f"Дата: {format_business_date(str(session['business_date']))}\n"
        f"Локация: {escape(str(session['source_location_name']))}\n"
        f"Базовый отчёт: версия {int(session['base_version'])}\n"
        f"Причина: {escape(str(session['reason_text']))}\n"
        f"Статус: {_status_label(str(session['status']))}\n"
        f"Revision: {int(session['revision'])}\n"
        f"Открыл: сотрудник, {escape(str(session['opened_at_utc']))}\n\n"
        f"Активных операций: {int(session['operation_count'])}\n"
        f"➕ Добавлено: {counts['added']}\n"
        f"✏️ Изменено: {counts['changed']}\n"
        f"🗑 Удалено: {counts['deleted']}\n"
        f"♻️ Восстановлено: {counts['restored']}\n"
        f"Checklist: {checklist}\n\n"
        + (
            "Новая версия отчёта ещё не выпущена."
            if session["status"] in {"open", "building", "failed"}
            else "Исправленная версия отчёта выпущена."
            if session["status"] == "completed"
            else "Исправление отменено."
        )
    )


def operation_text(operation: dict) -> str:
    kind = {
        "writeoff": "Списание", "transfer": "Перемещение",
        "development": "Проработка",
    }[str(operation["operation_type"])]
    state = {
        "unchanged": "без изменений", "added": "добавлено",
        "changed": "изменено", "deleted": "удалено",
        "restored": "восстановлено",
    }[str(operation["change_state"])]
    lines = [
        f"<b>{kind}</b>", "",
        f"Продукт: {escape(str(operation['product_name_snapshot']))}",
        f"Количество: {format_scaled(int(operation['quantity_scaled']), str(operation['base_unit']))}",
    ]
    if operation["operation_type"] == "transfer":
        lines.append(
            f"Направление: {escape(str(operation['source_location_name']))} → "
            f"{escape(str(operation.get('destination_location_name') or '—'))}"
        )
    if operation["operation_type"] == "development":
        lines.append(
            f"Проработка: {escape(str(operation.get('development_title') or '—'))}"
        )
    note = operation.get("reason_text") or operation.get("comment_text")
    if note:
        lines.append(f"Причина/комментарий: {escape(str(note))}")
    lines.extend((
        f"Revision: {int(operation['revision'])}",
        f"Состояние: {state}",
        f"Удалено: {'да' if operation['is_deleted'] else 'нет'}",
    ))
    base = operation.get("base")
    if base and operation["change_state"] in {"changed", "deleted", "restored"}:
        differences = _field_differences(base, operation)
        if differences:
            lines.extend(("", "<b>Отличия от исходной версии:</b>", *differences[:4]))
    return "\n".join(lines)


def _field_differences(base: dict, current: dict) -> list[str]:
    values: list[str] = []
    if (
        base.get("quantity_scaled") != current.get("quantity_scaled")
        or base.get("base_unit") != current.get("base_unit")
    ):
        values.append(
            "Количество: "
            f"{format_scaled(int(base['quantity_scaled']), str(base['base_unit']))} → "
            f"{format_scaled(int(current['quantity_scaled']), str(current['base_unit']))}"
        )
    for key, label in (
        ("product_name_snapshot", "Продукт"),
        ("reason_text", "Причина"),
        ("comment_text", "Комментарий"),
        ("development_title", "Название проработки"),
    ):
        if base.get(key) != current.get(key):
            values.append(
                f"{label}: {escape(str(base.get(key) or '—'))} → "
                f"{escape(str(current.get(key) or '—'))}"
            )
    if base.get("destination_location_id") != current.get("destination_location_id"):
        values.append(
            "Получатель перемещения изменён: "
            f"{escape(str(current.get('destination_location_name') or '—'))}"
        )
    return values


async def _dashboard(
    message: Message, service: WarehouseCorrectionService, session_id: int,
    actor_user_id: int,
) -> None:
    session = await _thread(service.dashboard, session_id, actor_user_id=actor_user_id)
    await edit_or_send_text(
        message, dashboard_text(session), correction_dashboard_keyboard(session)
    )


async def _operation_card(
    message: Message, service: WarehouseCorrectionService, operation_id: int,
    actor_user_id: int, *, page: int = 0,
) -> None:
    operation = await _thread(
        service.active_operation, operation_id, actor_user_id=actor_user_id
    )
    await edit_or_send_text(
        message, operation_text(operation),
        correction_operation_keyboard(operation, page=page),
    )


@router.message(Command("cancel"), StateFilter(WarehouseCorrectionStates))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. Активное исправление не отменено.")


@router.callback_query(WarehouseCallback.filter(F.action == "k0"))
async def cancel_input(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    await state.clear()
    await callback.answer("Действие отменено")
    if not isinstance(callback.message, Message):
        return
    if callback_data.aux == 1:
        try:
            await _dashboard(
                callback.message, warehouse_correction_service,
                callback_data.object_id, callback.from_user.id,
            )
        except Exception:
            await callback.message.answer("Действие отменено.")
    else:
        await callback.message.answer("Открытие исправления отменено.")


@router.callback_query(WarehouseCallback.filter(F.action == "ko"))
async def open_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        entry = await _thread(
            warehouse_correction_service.report_entry, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if entry["active_session_id"] is not None:
            await state.clear()
            await callback.answer()
            if isinstance(callback.message, Message):
                await _dashboard(
                    callback.message, warehouse_correction_service,
                    int(entry["active_session_id"]), callback.from_user.id,
                )
            return
        if not entry["can_open"] or int(entry["shift_revision"]) != callback_data.aux:
            raise WarehouseCorrectionConflict("Shift revision conflict")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        correction_report_id=int(entry["report_id"]),
        correction_shift_id=int(entry["shift_id"]),
        correction_shift_revision=int(entry["shift_revision"]),
    )
    await state.set_state(WarehouseCorrectionStates.open_reason)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "✏️ <b>Открытие исправления</b>\n\n"
            f"Дата: {format_business_date(str(entry['business_date']))}\n"
            f"Локация: {escape(str(entry['source_location_name']))}\n"
            f"Текущий отчёт: версия {int(entry['version'])}\n"
            f"Закрыт: {escape(str(entry['closed_at_utc'] or entry['ready_at_utc'] or '—'))}\n\n"
            "Введите обязательную причину исправления (1–500 символов)."
        )


@router.message(WarehouseCorrectionStates.open_reason, F.text)
async def open_reason(
    message: Message, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    reason = (message.text or "").strip()
    if not 1 <= len(reason) <= 500:
        await message.answer("Причина должна содержать от 1 до 500 символов.")
        return
    data = await state.get_data()
    try:
        entry = await _thread(
            warehouse_correction_service.report_entry,
            int(data["correction_report_id"]), actor_user_id=message.from_user.id,
        )
        if (
            not entry["can_open"]
            or int(entry["shift_revision"]) != int(data["correction_shift_revision"])
        ):
            raise WarehouseCorrectionConflict("Shift revision conflict")
    except Exception as exc:
        await state.clear()
        await message.answer(_safe_error(exc))
        return
    await state.update_data(correction_reason=reason)
    await state.set_state(WarehouseCorrectionStates.open_confirm)
    await message.answer(
        "<b>Проверьте открытие исправления</b>\n\n"
        f"Смена: {format_business_date(str(entry['business_date']))}, "
        f"{escape(str(entry['source_location_name']))}\n"
        f"Базовый отчёт: версия {int(entry['version'])}\n"
        f"Причина: {escape(reason)}\n\n"
        "Исходный отчёт останется неизменяемым.",
        reply_markup=correction_open_confirm_keyboard(
            int(entry["report_id"]), int(entry["shift_revision"])
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "kf"))
async def open_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    try:
        entry = await _thread(
            warehouse_correction_service.report_entry, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if entry["active_session_id"] is not None:
            session_id = int(entry["active_session_id"])
        else:
            if (
                await state.get_state() != WarehouseCorrectionStates.open_confirm.state
                or int(data.get("correction_report_id", 0)) != callback_data.object_id
                or int(data.get("correction_shift_revision", -1)) != callback_data.aux
                or not entry["can_open"]
                or int(entry["shift_revision"]) != callback_data.aux
            ):
                raise WarehouseCorrectionConflict("Shift revision conflict")
            session = await _thread(
                warehouse_correction_service.open_correction,
                int(entry["shift_id"]), str(data["correction_reason"]),
                expected_shift_revision=callback_data.aux,
                actor_user_id=callback.from_user.id,
            )
            session_id = int(session["id"])
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await _dashboard(
            callback.message, warehouse_correction_service,
            session_id, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kd"))
async def dashboard(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    await state.clear()
    try:
        if isinstance(callback.message, Message):
            await _dashboard(
                callback.message, warehouse_correction_service,
                callback_data.object_id, callback.from_user.id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "kl"))
async def operations_list(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    page = max(callback_data.aux, 0)
    try:
        rows, total = await _thread(
            warehouse_correction_service.operation_page,
            callback_data.object_id, page=page, actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "📋 <b>Операции исправления</b>\n\n"
            + ("Выберите операцию." if rows else "Операций нет."),
            correction_operations_keyboard(
                rows, session_id=callback_data.object_id, page=page, total=total
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kv"))
async def operation_card(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await _operation_card(
                callback.message, warehouse_correction_service,
                callback_data.object_id, callback.from_user.id,
                page=max(callback_data.aux, 0),
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


async def _begin_add(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    service: WarehouseCorrectionService, kind: str,
) -> None:
    session = await _thread(
        service.dashboard, callback_data.object_id, actor_user_id=callback.from_user.id
    )
    if (
        not session["can_manage"] or session["status"] != "open"
        or int(session["revision"]) != callback_data.aux
    ):
        raise WarehouseCorrectionConflict("Correction revision conflict")
    await state.clear()
    await state.update_data(
        correction_session_id=int(session["id"]),
        correction_session_revision=int(session["revision"]),
        correction_kind=kind,
        correction_source_location_id=int(session["source_location_id"]),
    )
    await state.set_state(WarehouseCorrectionStates.add_product)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите название активного продукта.")


@router.callback_query(WarehouseCallback.filter(F.action.in_({"kw", "kt", "kg"})))
async def add_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    kind = {"kw": "writeoff", "kt": "transfer", "kg": "development"}[
        callback_data.action
    ]
    try:
        await _begin_add(
            callback, callback_data, state, warehouse_correction_service, kind
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


async def _selected_product(
    message: Message, state: FSMContext, service: WarehouseOperationsService,
    product_id: int,
) -> None:
    product = await _thread(
        service.product_option, product_id, actor_user_id=message.from_user.id
    )
    if product is None:
        raise WarehouseCorrectionError("Active product not found")
    await state.update_data(
        correction_product_id=product.id,
        correction_product_name=product.official_name,
        correction_product_unit=product.base_unit.value,
    )
    current = await state.get_state()
    if current == WarehouseCorrectionStates.edit_product.state:
        await state.set_state(WarehouseCorrectionStates.edit_product_quantity)
        await message.answer(
            f"Введите новое количество для {escape(product.official_name)} "
            f"(основная единица: {product.base_unit.value})."
        )
    else:
        await state.set_state(WarehouseCorrectionStates.add_quantity)
        await message.answer(
            f"Введите количество для {escape(product.official_name)} "
            f"(например: 0,2 {product.base_unit.value})."
        )


@router.message(WarehouseCorrectionStates.add_product, F.text)
@router.message(WarehouseCorrectionStates.edit_product, F.text)
async def product_input(
    message: Message, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    try:
        session = await _thread(
            warehouse_correction_service.dashboard,
            int(data["correction_session_id"]), actor_user_id=message.from_user.id,
        )
        if not session["can_manage"] or session["status"] != "open":
            raise AccessDeniedError("Correction management required")
        result = await _thread(
            warehouse_service.find_product, message.text or "",
            str(data["correction_kind"]), actor_user_id=message.from_user.id,
        )
        if result.exact_id is not None:
            await _selected_product(message, state, warehouse_service, result.exact_id)
            return
        if result.candidates:
            action = "ku" if await state.get_state() == WarehouseCorrectionStates.edit_product.state else "kp"
            await message.answer(
                "Выберите точный продукт:",
                reply_markup=correction_candidates_keyboard(
                    result.candidates, action=action,
                    session_id=int(data["correction_session_id"]),
                ),
            )
            return
        await message.answer("Активный продукт не найден. Введите название точнее.")
    except Exception as exc:
        await message.answer(_safe_error(exc))


@router.callback_query(WarehouseCallback.filter(F.action.in_({"kp", "ku"})))
async def product_choice(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    expected = (
        WarehouseCorrectionStates.edit_product.state
        if callback_data.action == "ku" else WarehouseCorrectionStates.add_product.state
    )
    if (
        await state.get_state() != expected
        or int(data.get("correction_session_id", 0)) != callback_data.object_id
        or not isinstance(callback.message, Message)
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    try:
        await _selected_product(
            callback.message, state, warehouse_service, callback_data.aux
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


async def _validate_quantity(
    message: Message, state: FSMContext,
) -> tuple[str, str | None, int, str]:
    data = await state.get_data()
    quantity, unit = _quantity_parts(message.text or "")
    parsed = parse_quantity(quantity, unit, str(data["correction_product_unit"]))
    await state.update_data(
        correction_quantity=quantity, correction_unit=unit,
        correction_quantity_scaled=parsed.quantity_scaled,
    )
    return quantity, unit, parsed.quantity_scaled, parsed.base_unit.value


@router.message(WarehouseCorrectionStates.add_quantity, F.text)
async def add_quantity(message: Message, state: FSMContext) -> None:
    try:
        await _validate_quantity(message, state)
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    data = await state.get_data()
    if data["correction_kind"] == "transfer":
        await state.set_state(WarehouseCorrectionStates.add_destination)
        await message.answer("Введите локацию назначения.")
    elif data["correction_kind"] == "development":
        await state.set_state(WarehouseCorrectionStates.add_development_title)
        await message.answer("Введите название проработки (1–200 символов).")
    else:
        await state.set_state(WarehouseCorrectionStates.add_note)
        await message.answer("Введите причину списания или отправьте «-».")


async def _selected_destination(
    message: Message, state: FSMContext, service: WarehouseOperationsService,
    destination_id: int,
) -> None:
    options = await _thread(service.destination_options, actor_user_id=message.from_user.id)
    destination = next((item for item in options if item.id == destination_id), None)
    if destination is None:
        raise WarehouseCorrectionError("Destination not found")
    await state.update_data(
        correction_destination_id=destination.id,
        correction_destination_name=destination.name,
    )
    if await state.get_state() == WarehouseCorrectionStates.edit_destination.state:
        await _show_edit_preview(message, state)
    else:
        await state.set_state(WarehouseCorrectionStates.add_note)
        await message.answer("Введите комментарий к перемещению или отправьте «-».")


@router.message(WarehouseCorrectionStates.add_destination, F.text)
@router.message(WarehouseCorrectionStates.edit_destination, F.text)
async def destination_input(
    message: Message, state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    try:
        operation = data.get("correction_operation")
        source_id = (
            int(operation["source_location_id"])
            if operation else int(data["correction_source_location_id"])
        )
        result = await _thread(
            warehouse_service.find_destination, message.text or "",
            source_location_id=source_id, actor_user_id=message.from_user.id,
        )
        if result.exact_id is not None:
            await _selected_destination(message, state, warehouse_service, result.exact_id)
            return
        if result.candidates:
            action = "kj" if operation else "ki"
            await message.answer(
                "Выберите точную локацию:",
                reply_markup=correction_candidates_keyboard(
                    result.candidates, action=action,
                    session_id=int(data["correction_session_id"]),
                ),
            )
            return
        await message.answer("Локация не найдена. Введите название точнее.")
    except Exception as exc:
        await message.answer(_safe_error(exc))


@router.callback_query(WarehouseCallback.filter(F.action.in_({"ki", "kj"})))
async def destination_choice(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_service: WarehouseOperationsService,
) -> None:
    data = await state.get_data()
    expected = (
        WarehouseCorrectionStates.edit_destination.state
        if callback_data.action == "kj" else WarehouseCorrectionStates.add_destination.state
    )
    if (
        await state.get_state() != expected
        or int(data.get("correction_session_id", 0)) != callback_data.object_id
        or not isinstance(callback.message, Message)
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    try:
        await _selected_destination(
            callback.message, state, warehouse_service, callback_data.aux
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.message(WarehouseCorrectionStates.add_development_title, F.text)
async def add_development_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return
    await state.update_data(correction_development_title=title)
    await state.set_state(WarehouseCorrectionStates.add_note)
    await message.answer("Введите комментарий к проработке или отправьте «-».")


@router.message(WarehouseCorrectionStates.add_note, F.text)
async def add_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    if len(note) > 500:
        await message.answer("Максимальная длина — 500 символов.")
        return
    await state.update_data(correction_note=None if note == "-" else note)
    data = await state.get_data()
    await state.set_state(WarehouseCorrectionStates.add_confirm)
    kind = {"writeoff": "списание", "transfer": "перемещение", "development": "проработка"}[
        str(data["correction_kind"])
    ]
    details = [
        "<b>Проверьте новую операцию</b>", "",
        f"Тип: {kind}",
        f"Продукт: {escape(str(data['correction_product_name']))}",
        f"Количество: {format_scaled(int(data['correction_quantity_scaled']), str(data['correction_product_unit']))}",
    ]
    if data.get("correction_destination_name"):
        details.append(f"Куда: {escape(str(data['correction_destination_name']))}")
    if data.get("correction_development_title"):
        details.append(f"Проработка: {escape(str(data['correction_development_title']))}")
    details.append(f"Причина/комментарий: {escape(str(data.get('correction_note') or '—'))}")
    await message.answer(
        "\n".join(details),
        reply_markup=correction_mutation_confirm_keyboard(
            "ks", int(data["correction_session_id"]),
            int(data["correction_session_revision"]),
            session_id=int(data["correction_session_id"]),
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "ks"))
async def add_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.add_confirm.state
        or int(data.get("correction_session_id", 0)) != callback_data.object_id
        or int(data.get("correction_session_revision", -1)) != callback_data.aux
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    try:
        operation = await _thread(
            warehouse_correction_service.create_operation,
            callback_data.object_id,
            operation_type=str(data["correction_kind"]),
            product_id=int(data["correction_product_id"]),
            quantity_text=str(data["correction_quantity"]),
            unit_text=data.get("correction_unit"),
            destination_location_id=data.get("correction_destination_id"),
            reason_text=(data.get("correction_note") if data["correction_kind"] == "writeoff" else None),
            comment_text=(data.get("correction_note") if data["correction_kind"] != "writeoff" else None),
            development_title=data.get("correction_development_title"),
            expected_session_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Операция добавлена")
    if isinstance(callback.message, Message):
        await _operation_card(
            callback.message, warehouse_correction_service,
            int(operation["id"]), callback.from_user.id,
        )


async def _begin_edit(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    service: WarehouseCorrectionService,
) -> dict:
    operation = await _thread(
        service.active_operation, callback_data.object_id,
        actor_user_id=callback.from_user.id,
    )
    if (
        not operation["can_manage"] or operation["session_status"] != "open"
        or operation["is_deleted"] or int(operation["revision"]) != callback_data.aux
    ):
        raise WarehouseCorrectionConflict("Operation revision conflict")
    await state.clear()
    await state.update_data(
        correction_session_id=int(operation["session_id"]),
        correction_session_revision=int(operation["session_revision"]),
        correction_operation_id=int(operation["id"]),
        correction_operation_revision=int(operation["revision"]),
        correction_kind=str(operation["operation_type"]),
        correction_operation=operation,
    )
    return operation


@router.callback_query(WarehouseCallback.filter(F.action.in_({"k1", "k2", "k3", "k4", "k5"})))
async def edit_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        operation = await _begin_edit(
            callback, callback_data, state, warehouse_correction_service
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    target = {
        "k1": WarehouseCorrectionStates.edit_product,
        "k2": WarehouseCorrectionStates.edit_quantity,
        "k3": WarehouseCorrectionStates.edit_note,
        "k4": WarehouseCorrectionStates.edit_destination,
        "k5": WarehouseCorrectionStates.edit_development_title,
    }[callback_data.action]
    await state.set_state(target)
    prompts = {
        "k1": "Введите новое название продукта.",
        "k2": "Введите новое количество и, при необходимости, единицу.",
        "k3": "Введите новую причину/комментарий или «-», чтобы очистить.",
        "k4": "Введите новую локацию назначения.",
        "k5": "Введите новое название проработки (1–200 символов).",
    }
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Текущее значение открыто из revision {operation['revision']}.\n"
            + prompts[callback_data.action]
        )


@router.message(WarehouseCorrectionStates.edit_product_quantity, F.text)
async def edit_product_quantity(message: Message, state: FSMContext) -> None:
    try:
        await _validate_quantity(message, state)
        await state.update_data(correction_edit_kind="product")
        await _show_edit_preview(message, state)
    except Exception as exc:
        await message.answer(_safe_error(exc))


@router.message(WarehouseCorrectionStates.edit_quantity, F.text)
async def edit_quantity(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    operation = data["correction_operation"]
    quantity, unit = _quantity_parts(message.text or "")
    try:
        parsed = parse_quantity(quantity, unit, str(operation["base_unit"]))
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await state.update_data(
        correction_quantity=quantity, correction_unit=unit,
        correction_quantity_scaled=parsed.quantity_scaled,
        correction_product_unit=parsed.base_unit.value,
        correction_edit_kind="quantity",
    )
    await _show_edit_preview(message, state)


@router.message(WarehouseCorrectionStates.edit_note, F.text)
async def edit_note(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 500:
        await message.answer("Максимальная длина — 500 символов.")
        return
    await state.update_data(
        correction_note=None if value == "-" else value,
        correction_edit_kind="note",
    )
    await _show_edit_preview(message, state)


@router.message(WarehouseCorrectionStates.edit_development_title, F.text)
async def edit_development_title(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not 1 <= len(value) <= 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return
    await state.update_data(
        correction_development_title=value,
        correction_edit_kind="development_title",
    )
    await _show_edit_preview(message, state)


async def _show_edit_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    operation = data["correction_operation"]
    kind = data.get("correction_edit_kind")
    if kind is None and data.get("correction_destination_id"):
        kind = "destination"
        await state.update_data(correction_edit_kind=kind)
    if kind == "product":
        old = escape(str(operation["product_name_snapshot"]))
        new = escape(str(data["correction_product_name"]))
    elif kind == "quantity":
        old = format_scaled(int(operation["quantity_scaled"]), str(operation["base_unit"]))
        new = format_scaled(int(data["correction_quantity_scaled"]), str(data["correction_product_unit"]))
    elif kind == "note":
        old = escape(str(operation.get("reason_text") or operation.get("comment_text") or "—"))
        new = escape(str(data.get("correction_note") or "—"))
    elif kind == "destination":
        old = escape(str(operation.get("destination_location_name") or "—"))
        new = escape(str(data["correction_destination_name"]))
    else:
        old = escape(str(operation.get("development_title") or "—"))
        new = escape(str(data["correction_development_title"]))
    await state.set_state(WarehouseCorrectionStates.edit_confirm)
    await message.answer(
        f"<b>Проверьте изменение</b>\n\n{old} → {new}",
        reply_markup=correction_mutation_confirm_keyboard(
            "ky", int(operation["id"]), int(operation["revision"]),
            session_id=int(data["correction_session_id"]),
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "ky"))
async def edit_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.edit_confirm.state
        or int(data.get("correction_operation_id", 0)) != callback_data.object_id
        or int(data.get("correction_operation_revision", -1)) != callback_data.aux
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    common = {
        "expected_revision": callback_data.aux,
        "expected_session_revision": int(data["correction_session_revision"]),
        "actor_user_id": callback.from_user.id,
    }
    session_id = int(data["correction_session_id"])
    operation_id = callback_data.object_id
    try:
        kind = str(data["correction_edit_kind"])
        if kind == "product":
            await _thread(
                warehouse_correction_service.update_product_and_quantity,
                session_id, operation_id, int(data["correction_product_id"]),
                str(data["correction_quantity"]), data.get("correction_unit"), **common,
            )
        elif kind == "quantity":
            await _thread(
                warehouse_correction_service.update_quantity,
                session_id, operation_id, str(data["correction_quantity"]),
                data.get("correction_unit"), **common,
            )
        elif kind == "note":
            await _thread(
                warehouse_correction_service.update_note,
                session_id, operation_id,
                reason_text=(data.get("correction_note") if data["correction_kind"] == "writeoff" else None),
                comment_text=(data.get("correction_note") if data["correction_kind"] != "writeoff" else None),
                **common,
            )
        elif kind == "destination":
            await _thread(
                warehouse_correction_service.update_destination,
                session_id, operation_id, int(data["correction_destination_id"]), **common,
            )
        else:
            await _thread(
                warehouse_correction_service.rename_development,
                session_id, operation_id, str(data["correction_development_title"]), **common,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        if isinstance(callback.message, Message):
            try:
                await _operation_card(
                    callback.message, warehouse_correction_service,
                    operation_id, callback.from_user.id,
                )
            except Exception:
                pass
        return
    await state.clear()
    await callback.answer("Изменение сохранено")
    if isinstance(callback.message, Message):
        await _operation_card(
            callback.message, warehouse_correction_service,
            operation_id, callback.from_user.id,
        )


async def _delete_restore_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    service: WarehouseCorrectionService, *, restore: bool,
) -> None:
    operation = await _thread(
        service.active_operation, callback_data.object_id,
        actor_user_id=callback.from_user.id,
    )
    if (
        not operation["can_manage"]
        or bool(operation["is_deleted"]) != restore
        or int(operation["revision"]) != callback_data.aux
    ):
        raise WarehouseCorrectionConflict("Operation revision conflict")
    await state.clear()
    await state.update_data(
        correction_session_id=int(operation["session_id"]),
        correction_session_revision=int(operation["session_revision"]),
        correction_operation_id=int(operation["id"]),
        correction_operation_revision=int(operation["revision"]),
        correction_operation=operation,
    )
    await state.set_state(
        WarehouseCorrectionStates.restore_reason
        if restore else WarehouseCorrectionStates.delete_reason
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(
            ("Укажите причину восстановления" if restore else "Укажите причину удаления")
            + f" операции «{escape(str(operation['product_name_snapshot']))}» (1–500 символов)."
        )


@router.callback_query(WarehouseCallback.filter(F.action.in_({"kx", "kr"})))
async def delete_restore_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        await _delete_restore_start(
            callback, callback_data, state, warehouse_correction_service,
            restore=callback_data.action == "kr",
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.message(WarehouseCorrectionStates.delete_reason, F.text)
@router.message(WarehouseCorrectionStates.restore_reason, F.text)
async def delete_restore_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    if not 1 <= len(reason) <= 500:
        await message.answer("Причина должна содержать от 1 до 500 символов.")
        return
    restoring = await state.get_state() == WarehouseCorrectionStates.restore_reason.state
    await state.update_data(correction_reason=reason)
    await state.set_state(
        WarehouseCorrectionStates.restore_confirm
        if restoring else WarehouseCorrectionStates.delete_confirm
    )
    data = await state.get_data()
    operation = data["correction_operation"]
    await message.answer(
        f"{'Восстановить' if restoring else 'Удалить'} операцию "
        f"«{escape(str(operation['product_name_snapshot']))}»?\n"
        f"Причина: {escape(reason)}",
        reply_markup=correction_mutation_confirm_keyboard(
            "kb" if restoring else "kz", int(operation["id"]),
            int(operation["revision"]),
            session_id=int(data["correction_session_id"]),
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action.in_({"kz", "kb"})))
async def delete_restore_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    expected_state = (
        WarehouseCorrectionStates.restore_confirm.state
        if callback_data.action == "kb" else WarehouseCorrectionStates.delete_confirm.state
    )
    if (
        await state.get_state() != expected_state
        or int(data.get("correction_operation_id", 0)) != callback_data.object_id
        or int(data.get("correction_operation_revision", -1)) != callback_data.aux
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    method = (
        warehouse_correction_service.restore_operation
        if callback_data.action == "kb" else warehouse_correction_service.delete_operation
    )
    try:
        await _thread(
            method, int(data["correction_session_id"]), callback_data.object_id,
            expected_revision=callback_data.aux,
            expected_session_revision=int(data["correction_session_revision"]),
            reason=str(data["correction_reason"]),
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Операция восстановлена" if callback_data.action == "kb" else "Операция удалена")
    if isinstance(callback.message, Message):
        await _operation_card(
            callback.message, warehouse_correction_service,
            callback_data.object_id, callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "ke"))
async def changes(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        rows = await _thread(
            warehouse_correction_service.changes, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    groups = {name: [] for name in ("added", "changed", "deleted", "restored")}
    for row in rows:
        groups[str(row["change_state"])].append(row)
    lines = ["📝 <b>Изменения относительно базового отчёта</b>"]
    titles = {
        "added": "➕ Добавлено", "changed": "✏️ Изменено",
        "deleted": "🗑 Удалено", "restored": "♻️ Восстановлено",
    }
    for key, title in titles.items():
        if not groups[key]:
            continue
        lines.extend(("", f"<b>{title}</b>"))
        for row in groups[key]:
            lines.append(f"• {escape(str(row['product_name_snapshot']))}")
            if row.get("base"):
                lines.extend(f"  {value}" for value in _field_differences(row["base"], row))
    if len(lines) == 1:
        lines.extend(("", "Изменений пока нет."))
    await callback.answer()
    if isinstance(callback.message, Message):
        session = await _thread(
            warehouse_correction_service.dashboard, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        await edit_or_send_text(
            callback.message, "\n".join(lines),
            correction_dashboard_keyboard(session),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kn"))
async def cancel_session_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        session = await _thread(
            warehouse_correction_service.dashboard, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if (
            not session["can_manage"] or session["has_mutations"]
            or session["status"] != "open"
            or int(session["revision"]) != callback_data.aux
        ):
            raise WarehouseCorrectionConflict("Correction revision conflict")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        correction_session_id=int(session["id"]),
        correction_session_revision=int(session["revision"]),
    )
    await state.set_state(WarehouseCorrectionStates.cancel_session)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "<b>Отменить исправление?</b>\n\n"
            f"Смена: {format_business_date(str(session['business_date']))}\n"
            f"Базовая версия: {int(session['base_version'])}\n"
            f"Причина: {escape(str(session['reason_text']))}\n\n"
            "Смена вернётся в статус closed. Это возможно только до первой mutation.",
            reply_markup=correction_cancel_session_keyboard(
                int(session["id"]), int(session["revision"])
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "km"))
async def cancel_session_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.cancel_session.state
        or int(data.get("correction_session_id", 0)) != callback_data.object_id
        or int(data.get("correction_session_revision", -1)) != callback_data.aux
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    try:
        session = await _thread(
            warehouse_correction_service.cancel_correction,
            callback_data.object_id, expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Исправление отменено")
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "Исправление отменено. Смена возвращена в закрытое состояние.\n"
            f"Базовый отчёт версии {int(session['base_version'])} не изменён.",
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kh"))
async def future_stage(callback: CallbackQuery) -> None:
    await callback.answer(
        "Сначала внесите хотя бы одно изменение.",
        show_alert=True,
    )


def finalize_preview_text(preview: dict) -> str:
    counts = preview["change_counts"]
    snapshot = preview["snapshot_counts"]
    lines = [
        "📄 <b>Выпуск исправленного отчёта</b>", "",
        f"Дата: {format_business_date(str(preview['business_date']))}",
        f"Локация: {escape(str(preview['source_location_name']))}",
        f"Базовая версия: v{int(preview['base_version']):03d}",
        f"Новая версия: v{int(preview['future_version']):03d}",
        f"Причина: {escape(str(preview['reason_text']))}",
        f"Автор: {escape(str(preview['opened_by_name']))}",
        "",
        f"➕ Добавлено: {counts['added']}",
        f"✏️ Изменено: {counts['changed']}",
        f"🗑 Удалено: {counts['deleted']}",
        f"♻️ Восстановлено: {counts['restored']}",
        f"Операций в snapshot: {int(preview['snapshot_operation_count'])}",
        "",
        "Листы XLSX:",
        f"• Списание — {int(snapshot.get('writeoff', 0))}",
        f"• Перемещение — {int(snapshot.get('transfer', 0))}",
        f"• Проработка — {int(snapshot.get('development', 0))}",
    ]
    if preview["changes"]:
        lines.extend(("", "<b>Изменения:</b>"))
        for row in preview["changes"][:20]:
            marker = {
                "added": "➕", "changed": "✏️", "deleted": "🗑", "restored": "♻️"
            }[str(row["change_state"])]
            quantity = format_scaled(
                int(row["quantity_scaled"]), str(row["base_unit"])
            )
            lines.append(
                f"{marker} {escape(str(row['product_name_snapshot']))} — {quantity}"
            )
            if row.get("base"):
                lines.extend(
                    f"  {value}" for value in _field_differences(row["base"], row)[:3]
                )
        if len(preview["changes"]) > 20:
            lines.append(f"… и ещё {len(preview['changes']) - 20}")
    if preview["missing"]:
        lines.extend(("", "⛔ <b>Выпуск заблокирован</b>"))
        lines.extend(
            f"• {escape(str(row['product_name']))}" for row in preview["missing"]
        )
    else:
        lines.extend(("", "✅ Required checklist выполнен."))
    lines.extend(("", "Старые версии и их файлы останутся неизменяемыми."))
    return "\n".join(lines)


@router.callback_query(WarehouseCallback.filter(F.action == "k6"))
async def finalize_preview(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        preview = await _thread(
            warehouse_correction_service.finalize_preview,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
        if int(preview["revision"]) != callback_data.aux:
            raise WarehouseCorrectionConflict("Correction revision conflict")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        correction_session_id=int(preview["id"]),
        correction_session_revision=int(preview["revision"]),
    )
    await state.set_state(WarehouseCorrectionStates.finalize_confirm)
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message, finalize_preview_text(preview),
            correction_finalize_keyboard(
                int(preview["id"]), int(preview["revision"]),
                blocked=bool(preview["missing"]),
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kq"))
async def finalize_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await edit_or_send_text(
                callback.message, "⏳ Формируется исправленный отчёт…"
            )
        result = await _thread(
            warehouse_correction_service.release_correction,
            callback_data.object_id, expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
        if not result["ready"]:
            await callback.answer("Required checklist не выполнен.", show_alert=True)
            if isinstance(callback.message, Message):
                preview = await _thread(
                    warehouse_correction_service.finalize_preview,
                    callback_data.object_id, actor_user_id=callback.from_user.id,
                )
                await edit_or_send_text(
                    callback.message, finalize_preview_text(preview),
                    correction_finalize_keyboard(
                        callback_data.object_id, int(preview["revision"]), blocked=True
                    ),
                )
            return
    except Exception as exc:
        await state.clear()
        await callback.answer(_safe_error(exc), show_alert=True)
        if isinstance(callback.message, Message):
            try:
                session = await _thread(
                    warehouse_correction_service.dashboard,
                    callback_data.object_id, actor_user_id=callback.from_user.id,
                )
                await edit_or_send_text(
                    callback.message,
                    "⚠️ Выпуск не завершён. Состояние сохранено; проверьте выпуск.",
                    correction_recovery_keyboard(
                        callback_data.object_id, failed=session["status"] == "failed"
                    ),
                )
            except Exception:
                pass
        return
    await state.clear()
    await callback.answer("Исправленный отчёт готов")
    if isinstance(callback.message, Message):
        await _show_result(
            callback.message, warehouse_correction_service,
            warehouse_delivery_service, callback_data.object_id,
            callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "k8"))
async def recovery_preview(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        session = await _thread(
            warehouse_correction_service.recovery_preview, callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        if session["status"] == "completed":
            await callback.answer()
            return
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "🔄 <b>Восстановление выпуска</b>\n\n"
            f"Текущее состояние: {_status_label(str(session['status']))}.\n"
            "Будет проверена та же logical version; новая версия не создаётся.",
            correction_recovery_keyboard(
                callback_data.object_id, failed=session["status"] == "failed"
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "ka"))
async def recovery_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await edit_or_send_text(callback.message, "⏳ Проверяю выпуск отчёта…")
        await _thread(
            warehouse_correction_service.recover_correction,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Выпуск восстановлен")
    if isinstance(callback.message, Message):
        await _show_result(
            callback.message, warehouse_correction_service,
            warehouse_delivery_service, callback_data.object_id,
            callback.from_user.id,
        )


async def _show_result(
    message: Message,
    correction_service: WarehouseCorrectionService,
    delivery_service: WarehouseReportDeliveryService,
    session_id: int,
    actor_user_id: int,
) -> None:
    result = await _thread(
        correction_service.result, session_id, actor_user_id=actor_user_id
    )
    delivery = await _thread(
        delivery_service.history, int(result["report"]["id"]),
        actor_user_id=actor_user_id,
    )
    has_active_failed = any(
        row["status"] == "failed" and row.get("recipient_is_active", 1)
        for row in delivery["details"]
    )
    session = result["session"]
    report = result["report"]
    text = (
        "✅ <b>Исправленный отчёт готов</b>\n\n"
        f"Дата: {format_business_date(str(report['business_date']))}\n"
        f"Версия: v{int(report['version']):03d}\n"
        f"Базовая версия: v{int(session['base_version']):03d}\n"
        f"Причина: {escape(str(session['reason_text']))}\n"
        f"Создан: {escape(str(report['ready_at_utc'] or report['created_at_utc']))}\n"
        f"Списание: {int(report['writeoff_count'])}\n"
        f"Перемещение: {int(report['transfer_count'])}\n"
        f"Проработка: {int(report['development_count'])}\n"
        f"Файл: {escape(str(result['file_name']))}\n"
        f"Состояние выпуска: {_status_label(str(session['status']))}\n"
        f"Доставка: ✅ {delivery['delivered']}, ⚠️ {delivery['failed']}, "
        f"⏳ {delivery['pending']}"
    )
    await edit_or_send_text(
        message, text,
        correction_result_keyboard(
            session_id, int(report["id"]),
            can_manage=bool(result["can_manage"]),
            has_failed=has_active_failed,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "k9"))
async def result_card(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await _show_result(
                callback.message, warehouse_correction_service,
                warehouse_delivery_service, callback_data.object_id,
                callback.from_user.id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "kk"))
async def result_download(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        result = await _thread(
            warehouse_correction_service.download_result,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer_document(
            FSInputFile(
                result["artifact"].path,
                filename=Path(result["artifact"].relative_file_path).name,
            ),
            caption=(
                f"Исправленный отчёт за "
                f"{format_business_date(str(result['report']['business_date']))}, "
                f"v{int(result['report']['version']):03d}."
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kH"))
async def version_history(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        result = await _thread(
            warehouse_correction_service.version_history,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    lines = [
        "🕘 <b>История версий</b>",
        format_business_date(str(result["session"]["business_date"])),
    ]
    for version in result["versions"]:
        kind = "Исправленная версия" if version["correction_session_id"] else "Исходный отчёт"
        lines.extend((
            "",
            f"<b>v{int(version['version']):03d}</b> — {kind}",
            f"Статус: {escape(str(version['status']))}",
            f"Создан: {escape(str(version['ready_at_utc'] or version['created_at_utc']))}",
        ))
        if version["correction_reason"]:
            lines.append(f"Причина: {escape(str(version['correction_reason']))}")
            lines.append(
                f"Базовая версия: v{int(version['supersedes_version']):03d}"
            )
            lines.append(f"Автор: {escape(str(version['opened_by_name']))}")
        lines.append(
            f"Доставка: ✅ {int(version['delivered_count'])}, "
            f"⚠️ {int(version['failed_count'])}, ⏳ {int(version['pending_count'])}"
        )
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message, "\n".join(lines),
            correction_versions_keyboard(
                result["versions"], session_id=callback_data.object_id
            ),
        )


async def _recipient_page(
    message: Message, state: FSMContext,
    service: WarehouseCorrectionService, actor_user_id: int, *, page: int = 0,
) -> bool:
    data = await state.get_data()
    session_id = int(data["correction_delivery_session_id"])
    preview = await _thread(
        service.delivery_preview, session_id, actor_user_id=actor_user_id
    )
    active = preview["active_recipients"]
    active_ids = {int(row["id"]) for row in active}
    selected = {int(value) for value in data.get("correction_recipient_ids", [])}
    refreshed = selected & active_ids
    changed = refreshed != selected
    await state.update_data(correction_recipient_ids=sorted(refreshed))
    page = max(0, min(page, max(0, (len(active) - 1) // 8)))
    rows = active[page * 8:(page + 1) * 8]
    await edit_or_send_text(
        message,
        "👥 <b>Получатели исправленного отчёта</b>\n\n"
        f"Выбрано: {len(refreshed)} · страница {page + 1}",
        correction_recipient_keyboard(
            rows, session_id=session_id, selected=refreshed,
            page=page, total=len(active),
        ),
    )
    return changed


@router.callback_query(WarehouseCallback.filter(F.action == "kR"))
async def recipient_start(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        preview = await _thread(
            warehouse_correction_service.delivery_preview,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
        if not preview["active_recipients"]:
            raise WarehouseCorrectionError("No active recipients")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        correction_delivery_session_id=callback_data.object_id,
        correction_recipient_ids=list(preview["recipient_ids"]),
    )
    await state.set_state(WarehouseCorrectionStates.recipient_selection)
    await callback.answer()
    if isinstance(callback.message, Message):
        await _recipient_page(
            callback.message, state, warehouse_correction_service,
            callback.from_user.id,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kB"))
async def recipient_back(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    await state.clear()
    try:
        if isinstance(callback.message, Message):
            await _show_result(
                callback.message, warehouse_correction_service,
                warehouse_delivery_service, callback_data.object_id,
                callback.from_user.id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "kT"))
async def recipient_toggle(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if await state.get_state() != WarehouseCorrectionStates.recipient_selection.state:
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    selected = {int(value) for value in data.get("correction_recipient_ids", [])}
    selected.symmetric_difference_update({callback_data.object_id})
    await state.update_data(correction_recipient_ids=sorted(selected))
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await _recipient_page(
                callback.message, state, warehouse_correction_service,
                callback.from_user.id, page=callback_data.aux,
            )
        except Exception as exc:
            await callback.answer(_safe_error(exc), show_alert=True)


@router.callback_query(WarehouseCallback.filter(F.action == "kL"))
async def recipient_page(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if (
        int(data.get("correction_delivery_session_id", 0)) != callback_data.object_id
        or await state.get_state() not in {
            WarehouseCorrectionStates.recipient_selection.state,
            WarehouseCorrectionStates.delivery_confirm.state,
        }
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    await state.set_state(WarehouseCorrectionStates.recipient_selection)
    try:
        if isinstance(callback.message, Message):
            await _recipient_page(
                callback.message, state, warehouse_correction_service,
                callback.from_user.id, page=callback_data.aux,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "kV"))
async def delivery_preview(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.recipient_selection.state
        or int(data.get("correction_delivery_session_id", 0)) != callback_data.object_id
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    selected = tuple(int(value) for value in data.get("correction_recipient_ids", []))
    try:
        recipients = await _thread(
            warehouse_correction_service.validate_delivery_selection,
            callback_data.object_id, selected, actor_user_id=callback.from_user.id,
        )
        preview = await _thread(
            warehouse_correction_service.delivery_preview,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
    except WarehouseCorrectionConflict:
        await callback.answer("Список получателей изменился.", show_alert=True)
        if isinstance(callback.message, Message):
            await _recipient_page(
                callback.message, state, warehouse_correction_service,
                callback.from_user.id,
            )
        return
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseCorrectionStates.delivery_confirm)
    text = (
        "📨 <b>Отправка исправленного отчёта</b>\n\n"
        f"Дата: {format_business_date(str(preview['business_date']))}\n"
        f"Версия: v{int(preview['version']):03d}\n"
        f"Базовая версия: v{int(preview['base_version']):03d}\n"
        f"Причина: {escape(str(preview['reason']))}\n"
        f"Получателей: {len(recipients)}\n\n"
        + "\n".join(f"• {escape(str(row['title']))}" for row in recipients)
        + "\n\nЭто отдельная доставка новой immutable версии."
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message, text,
            correction_delivery_confirm_keyboard(callback_data.object_id),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kS"))
async def delivery_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.delivery_confirm.state
        or int(data.get("correction_delivery_session_id", 0)) != callback_data.object_id
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    selected = tuple(int(value) for value in data.get("correction_recipient_ids", []))
    try:
        await _thread(
            warehouse_correction_service.validate_delivery_selection,
            callback_data.object_id, selected, actor_user_id=callback.from_user.id,
        )
    except WarehouseCorrectionConflict:
        await state.set_state(WarehouseCorrectionStates.recipient_selection)
        await callback.answer("Список получателей изменился.", show_alert=True)
        if isinstance(callback.message, Message):
            await _recipient_page(
                callback.message, state, warehouse_correction_service,
                callback.from_user.id,
            )
        return
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    try:
        result = await warehouse_correction_service.deliver_correction(
            callback_data.object_id, warehouse_delivery_service,
            actor_user_id=callback.from_user.id, recipient_ids=selected,
        )
    except WarehouseCorrectionConflict:
        await state.set_state(WarehouseCorrectionStates.recipient_selection)
        await callback.answer("Список получателей изменился.", show_alert=True)
        if isinstance(callback.message, Message):
            await _recipient_page(
                callback.message, state, warehouse_correction_service,
                callback.from_user.id,
            )
        return
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer(
        f"Отправлено: {result['delivered']}, ошибок: {result['failed']}"
    )
    if isinstance(callback.message, Message):
        await _show_delivery_status(
            callback.message, warehouse_correction_service,
            warehouse_delivery_service, callback_data.object_id,
            callback.from_user.id,
        )


async def _show_delivery_status(
    message: Message,
    correction_service: WarehouseCorrectionService,
    delivery_service: WarehouseReportDeliveryService,
    session_id: int,
    actor_user_id: int,
) -> None:
    result = await _thread(
        correction_service.result, session_id, actor_user_id=actor_user_id
    )
    delivery = await _thread(
        delivery_service.history, int(result["report"]["id"]),
        actor_user_id=actor_user_id,
    )
    labels = {
        "delivered": "sent", "failed": "failed", "pending": "pending",
        "sending": "pending",
    }
    details = []
    skipped = 0
    for row in delivery["details"]:
        status = labels[str(row["status"])]
        if row["status"] == "failed" and not row.get("recipient_is_active", 1):
            status = "skipped"
            skipped += 1
        details.append(f"• {escape(str(row['recipient_title']))}: {status}")
    has_active_failed = any(
        row["status"] == "failed" and row.get("recipient_is_active", 1)
        for row in delivery["details"]
    )
    text = (
        "📋 <b>Статусы доставки исправленного отчёта</b>\n\n"
        f"sent: {delivery['delivered']}\n"
        f"failed: {delivery['failed'] - skipped}\n"
        f"skipped: {skipped}\n"
        f"pending: {delivery['pending']}"
    )
    if details:
        text += "\n\n" + "\n".join(details)
    await edit_or_send_text(
        message, text,
        correction_result_keyboard(
            session_id, int(result["report"]["id"]),
            can_manage=bool(result["can_manage"]),
            has_failed=has_active_failed,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "kD"))
async def delivery_status(
    callback: CallbackQuery, callback_data: WarehouseCallback,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await _show_delivery_status(
                callback.message, warehouse_correction_service,
                warehouse_delivery_service, callback_data.object_id,
                callback.from_user.id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "kY"))
async def retry_preview(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        preview = await _thread(
            warehouse_correction_service.retry_preview,
            callback_data.object_id, actor_user_id=callback.from_user.id,
        )
        if not preview["recipients"]:
            raise WarehouseCorrectionError("No active failed deliveries")
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(correction_delivery_session_id=callback_data.object_id)
    await state.set_state(WarehouseCorrectionStates.retry_confirm)
    text = (
        "🔄 <b>Повторить неуспешные доставки?</b>\n\n"
        + "\n".join(
            f"• {escape(str(row['title']))}" for row in preview["recipients"]
        )
    )
    if preview["skipped_inactive"]:
        text += f"\n\nНеактивные получатели будут пропущены: {preview['skipped_inactive']}"
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message, text,
            correction_retry_keyboard(callback_data.object_id, confirm=True),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "kZ"))
async def retry_confirm(
    callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext,
    warehouse_correction_service: WarehouseCorrectionService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != WarehouseCorrectionStates.retry_confirm.state
        or int(data.get("correction_delivery_session_id", 0)) != callback_data.object_id
    ):
        await callback.answer("Этот экран устарел.", show_alert=True)
        return
    try:
        result = await warehouse_correction_service.retry_correction_delivery(
            callback_data.object_id, warehouse_delivery_service,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer(
        f"Доставлено: {result['delivered']}, ошибок: {result['failed']}"
    )
    if isinstance(callback.message, Message):
        await _show_delivery_status(
            callback.message, warehouse_correction_service,
            warehouse_delivery_service, callback_data.object_id,
            callback.from_user.id,
        )


__all__ = ["dashboard_text", "operation_text", "router"]
