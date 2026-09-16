"""Administrative Telegram UI for warehouse shift reminders."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import WarehouseReminderStates
from app.keyboards.warehouse import reminder_settings_keyboard
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError
from app.services.warehouse.recipient_service import parse_numeric_chat_id
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.telegram_delivery import TelegramChatVerificationError, TelegramChatVerifier
from app.storage.warehouse_reminder_database import WarehouseReminderConflict

router = Router(name=__name__)


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def _error(exc: Exception) -> str:
    if isinstance(exc, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(exc, WarehouseReminderConflict):
        return "Данные уже изменились. Откройте настройки заново."
    if isinstance(exc, (ValueError, TelegramChatVerificationError)):
        return escape(str(exc))
    return "Не удалось сохранить настройки. Попробуйте ещё раз."


async def _source(warehouse_service: WarehouseOperationsService) -> int:
    settings = await _thread(warehouse_service.repository.runtime_settings)
    return int(settings["source_location_id"])


def _text(settings: dict) -> str:
    return (
        "🔔 <b>Напоминания о незакрытой смене</b>\n\n"
        f"Статус: <b>{'включены' if settings['enabled'] else 'выключены'}</b>\n"
        f"Рабочий чат: {escape(str(settings['target_title'] or 'не выбран'))}\n"
        f"Timezone: <code>{escape(str(settings['timezone']))}</code>\n"
        f"Первое напоминание: за {settings['first_reminder_minutes_before_close']} мин.\n"
        f"Интервал: {settings['repeat_interval_minutes']} мин.\n"
        f"Максимум повторов: {settings['max_reminders']}"
    )


async def _show(message: Message, service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService, actor_user_id: int) -> None:
    source = await _source(warehouse_service)
    settings = await _thread(service.get, source, actor_user_id)
    await edit_or_send_text(message, _text(settings), reminder_settings_keyboard(settings))


@router.callback_query(WarehouseCallback.filter(F.action == "mr"))
async def reminders(callback: CallbackQuery, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    try:
        if isinstance(callback.message, Message):
            await state.clear()
            await _show(callback.message, warehouse_reminder_settings_service, warehouse_service, callback.from_user.id)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "me"))
async def toggle(callback: CallbackQuery, callback_data: WarehouseCallback, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    try:
        source = await _source(warehouse_service)
        current = await _thread(warehouse_reminder_settings_service.get, source, callback.from_user.id)
        updated = await _thread(warehouse_reminder_settings_service.update, source, {"enabled": 0 if current["enabled"] else 1}, callback_data.aux, callback.from_user.id)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    await callback.answer("Статус обновлён")
    if isinstance(callback.message, Message):
        await edit_or_send_text(callback.message, _text(updated), reminder_settings_keyboard(updated))


async def _store_target(message: Message, state: FSMContext, service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService, verifier: TelegramChatVerifier, actor_user_id: int, chat_id: int, expected_revision: int) -> None:
    candidate = await verifier.verify(chat_id)
    source = await _source(warehouse_service)
    updated = await _thread(service.update, source, {"target_chat_id": candidate.chat_id, "target_title": candidate.title}, expected_revision, actor_user_id)
    await state.clear()
    await edit_or_send_text(message, _text(updated), reminder_settings_keyboard(updated))


@router.callback_query(WarehouseCallback.filter(F.action == "mc"))
async def current_chat(callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService, warehouse_chat_verifier: TelegramChatVerifier) -> None:
    try:
        if not isinstance(callback.message, Message):
            raise ValueError("Не удалось определить текущий чат")
        await _store_target(callback.message, state, warehouse_reminder_settings_service, warehouse_service, warehouse_chat_verifier, callback.from_user.id, int(callback.message.chat.id), callback_data.aux)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    await callback.answer("Рабочий чат сохранён")


@router.callback_query(WarehouseCallback.filter(F.action == "mi"))
async def numeric_chat_prompt(callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    try:
        source = await _source(warehouse_service)
        await _thread(warehouse_reminder_settings_service.get, source, callback.from_user.id)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseReminderStates.chat_id)
    await state.update_data(reminder_revision=callback_data.aux)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите числовой Telegram chat ID. @username не поддерживается.")


@router.message(WarehouseReminderStates.chat_id, F.text)
async def numeric_chat_input(message: Message, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService, warehouse_chat_verifier: TelegramChatVerifier) -> None:
    if message.from_user is None:
        return
    try:
        data = await state.get_data()
        await _store_target(message, state, warehouse_reminder_settings_service, warehouse_service, warehouse_chat_verifier, message.from_user.id, parse_numeric_chat_id(message.text or ""), int(data["reminder_revision"]))
    except Exception as exc:
        await message.answer(_error(exc))


_INPUTS = {"mf": (WarehouseReminderStates.first, "first", "Введите минуты до закрытия: от 0 до 1440."), "mn": (WarehouseReminderStates.repeat, "repeat", "Введите интервал в минутах: от 5 до 1440."), "mm": (WarehouseReminderStates.maximum, "maximum", "Введите максимум повторов: от 1 до 10.")}


@router.callback_query(WarehouseCallback.filter(F.action.in_(set(_INPUTS))))
async def number_prompt(callback: CallbackQuery, callback_data: WarehouseCallback, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    try:
        source = await _source(warehouse_service)
        await _thread(warehouse_reminder_settings_service.get, source, callback.from_user.id)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    target_state, key, prompt = _INPUTS[callback_data.action]
    await state.set_state(target_state)
    await state.update_data(reminder_revision=callback_data.aux, reminder_key=key)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)


@router.message(StateFilter(WarehouseReminderStates.first, WarehouseReminderStates.repeat, WarehouseReminderStates.maximum), F.text)
async def number_input(message: Message, state: FSMContext, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    if message.from_user is None:
        return
    try:
        value = int((message.text or "").strip())
        data = await state.get_data()
        source = await _source(warehouse_service)
        updated = await _thread(warehouse_reminder_settings_service.update, source, {str(data["reminder_key"]): value}, int(data["reminder_revision"]), message.from_user.id)
    except Exception as exc:
        await message.answer(_error(exc))
        return
    await state.clear()
    await message.answer(_text(updated), reply_markup=reminder_settings_keyboard(updated))


@router.callback_query(WarehouseCallback.filter(F.action == "mh"))
async def history(callback: CallbackQuery, warehouse_reminder_settings_service: WarehouseReminderSettingsService, warehouse_service: WarehouseOperationsService) -> None:
    try:
        source = await _source(warehouse_service)
        await _thread(warehouse_reminder_settings_service.get, source, callback.from_user.id)
        rows = await _thread(warehouse_reminder_settings_service.repository.events, source)
    except Exception as exc:
        await callback.answer(_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        body = "\n".join(f"• {escape(str(row['business_date']))}: #{row['sequence_number']} — {escape(str(row['status']))}" for row in rows) or "Событий ещё нет."
        await edit_or_send_text(callback.message, f"🧾 <b>Последние события</b>\n\n{body}", reminder_settings_keyboard(await _thread(warehouse_reminder_settings_service.get, source, callback.from_user.id)))


__all__ = ["router"]
