"""Administrative Telegram UI for warehouse report recipients."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import WarehouseRecipientStates
from app.keyboards.warehouse import (
    report_recipient_candidate_keyboard,
    report_recipient_card_keyboard,
    report_recipients_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError
from app.services.warehouse.recipient_service import (
    WarehouseRecipientService,
    parse_numeric_chat_id,
)
from app.services.warehouse.telegram_delivery import (
    TelegramChatVerificationError,
    TelegramChatVerifier,
    VerifiedTelegramChat,
)
from app.storage.warehouse_delivery_database import (
    WarehouseDeliveryError,
    WarehouseRecipientConflict,
)

router = Router(name=__name__)


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def _safe_error(error: Exception) -> str:
    if isinstance(error, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(error, WarehouseRecipientConflict):
        return "Данные уже изменились или этот чат уже добавлен. Обновите список."
    if isinstance(error, (ValueError, TelegramChatVerificationError)):
        return escape(str(error))
    if isinstance(error, WarehouseDeliveryError):
        return "Получатель недоступен. Обновите список."
    return "Не удалось сохранить получателя. Попробуйте ещё раз."


def _candidate_text(candidate: VerifiedTelegramChat) -> str:
    return (
        "Проверьте найденный чат:\n\n"
        f"<b>{escape(candidate.title)}</b>\n"
        f"Тип: {escape(candidate.chat_type)}\n"
        f"Chat ID: <code>{candidate.chat_id}</code>\n\n"
        "Файл пока не отправляется. Сохранить получателя?"
    )


def _recipient_text(recipient: dict) -> str:
    status = "активен" if recipient["is_active"] else "неактивен"
    return (
        f"👥 <b>{escape(str(recipient['title']))}</b>\n\n"
        f"Chat ID: <code>{recipient['chat_id']}</code>\n"
        f"Тип: {escape(str(recipient['chat_type']))}\n"
        f"Статус: {status}"
    )


async def _show_list(
    message: Message,
    state: FSMContext,
    service: WarehouseRecipientService,
    actor_user_id: int,
) -> None:
    recipients = await _thread(service.list_recipients, actor_user_id=actor_user_id)
    await state.clear()
    text = "👥 <b>Получатели отчётов</b>"
    if not recipients:
        text += "\n\nПолучатели ещё не добавлены."
    await edit_or_send_text(message, text, report_recipients_keyboard(recipients))


async def _verify_and_confirm(
    message: Message,
    state: FSMContext,
    verifier: TelegramChatVerifier,
    chat_id: int,
) -> None:
    candidate = await verifier.verify(chat_id)
    await state.update_data(
        recipient_candidate={
            "chat_id": candidate.chat_id,
            "title": candidate.title,
            "chat_type": candidate.chat_type,
        }
    )
    await edit_or_send_text(
        message,
        _candidate_text(candidate),
        report_recipient_candidate_keyboard(),
    )


@router.message(Command("cancel"), StateFilter(WarehouseRecipientStates))
async def recipient_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление получателя отменено.")


@router.callback_query(WarehouseCallback.filter(F.action == "pr"))
async def recipient_list(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await _show_list(
                callback.message, state, warehouse_recipient_service,
                callback.from_user.id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "pc"))
async def recipient_add_current_chat(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
    warehouse_chat_verifier: TelegramChatVerifier,
) -> None:
    try:
        await _thread(
            warehouse_recipient_service.list_recipients,
            actor_user_id=callback.from_user.id,
        )
        if not isinstance(callback.message, Message):
            raise ValueError("Не удалось определить текущий чат")
        await _verify_and_confirm(
            callback.message, state, warehouse_chat_verifier,
            int(callback.message.chat.id),
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "pi"))
async def recipient_add_by_id(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        await _thread(
            warehouse_recipient_service.list_recipients,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseRecipientStates.chat_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите числовой Telegram chat ID. @username не поддерживается.\n"
            "Бот проверит чат и права на отправку документов до сохранения."
        )


@router.message(WarehouseRecipientStates.chat_id, F.text)
async def recipient_chat_id_input(
    message: Message,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
    warehouse_chat_verifier: TelegramChatVerifier,
) -> None:
    if message.from_user is None:
        return
    try:
        await _thread(
            warehouse_recipient_service.list_recipients,
            actor_user_id=message.from_user.id,
        )
        chat_id = parse_numeric_chat_id(message.text or "")
        await _verify_and_confirm(message, state, warehouse_chat_verifier, chat_id)
    except Exception as exc:
        await message.answer(_safe_error(exc))


@router.callback_query(WarehouseCallback.filter(F.action == "ps"))
async def recipient_save(
    callback: CallbackQuery,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    data = await state.get_data()
    candidate = data.get("recipient_candidate")
    if not isinstance(candidate, dict):
        await callback.answer("Подтверждение устарело. Проверьте чат заново.", show_alert=True)
        return
    try:
        recipient = await _thread(
            warehouse_recipient_service.create_verified,
            chat_id=int(candidate["chat_id"]),
            title=str(candidate["title"]),
            chat_type=str(candidate["chat_type"]),
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Получатель сохранён")
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            _recipient_text(recipient),
            report_recipient_card_keyboard(recipient),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "pv"))
async def recipient_card(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        recipient = await _thread(
            warehouse_recipient_service.recipient,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            _recipient_text(recipient),
            report_recipient_card_keyboard(recipient),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "pt"))
async def recipient_title_prompt(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        await _thread(
            warehouse_recipient_service.recipient,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await state.set_state(WarehouseRecipientStates.title)
    await state.update_data(
        recipient_id=callback_data.object_id,
        recipient_revision=callback_data.aux,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите новое название (до 200 символов).")


@router.message(WarehouseRecipientStates.title, F.text)
async def recipient_title_input(
    message: Message,
    state: FSMContext,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    try:
        recipient = await _thread(
            warehouse_recipient_service.rename,
            int(data["recipient_id"]),
            message.text or "",
            expected_revision=int(data["recipient_revision"]),
            actor_user_id=message.from_user.id,
        )
    except Exception as exc:
        await message.answer(_safe_error(exc))
        return
    await state.clear()
    await message.answer(
        _recipient_text(recipient),
        reply_markup=report_recipient_card_keyboard(recipient),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "pa"))
async def recipient_toggle(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        current = await _thread(
            warehouse_recipient_service.recipient,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
        recipient = await _thread(
            warehouse_recipient_service.set_active,
            callback_data.object_id,
            is_active=not bool(current["is_active"]),
            expected_revision=callback_data.aux,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer("Статус обновлён")
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            _recipient_text(recipient),
            report_recipient_card_keyboard(recipient),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "px"))
async def recipient_help(
    callback: CallbackQuery,
    warehouse_recipient_service: WarehouseRecipientService,
) -> None:
    try:
        await _thread(
            warehouse_recipient_service.list_recipients,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "<b>Как добавить бухгалтерскую группу</b>\n\n"
            "1. Добавьте бота в группу.\n"
            "2. Разрешите ему отправлять документы.\n"
            "3. Откройте /warehouse прямо в этой группе.\n"
            "4. Выберите «Получатели отчётов» → «Добавить этот чат».\n\n"
            "Можно также ввести числовой chat ID. @username не используется.",
            report_recipients_keyboard([]),
        )


__all__ = ["router"]
