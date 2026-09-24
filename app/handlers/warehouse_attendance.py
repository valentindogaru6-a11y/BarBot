"""Telegram flow for opening and closing personal work sessions."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.handlers.common import edit_or_send_text
from app.handlers.start import WELCOME_TEXT, _main_menu_for
from app.keyboards.callbacks import MainCallback
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.attendance_xlsx_service import WarehouseAttendanceXlsxService, parse_month
from app.storage.warehouse_attendance_database import WarehouseAttendanceError

router = Router(name=__name__)
ZONE = ZoneInfo("Europe/Kyiv")


class AttendanceStates(StatesGroup):
    report_month = State()
    force_reason = State()


def timesheet_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for action, text in (("td","📅 Сегодня"),("tm","🗓 Текущий месяц"),
                         ("tp","🗓 Предыдущий месяц"),("ti","✍️ Выбрать месяц")):
        builder.button(text=text, callback_data=WarehouseCallback(action=action))
    builder.button(text="⬅️ Назад", callback_data=WarehouseCallback(action="h"))
    builder.adjust(1); return builder.as_markup()


def attendance_confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Открыть" if action == "aO" else "✅ Закрыть",
                   callback_data=WarehouseCallback(action=action))
    builder.button(text="⬅️ Отмена" if action == "aO" else "⬅️ Продолжить смену",
                   callback_data=MainCallback(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def last_participant_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Перейти к закрытию склада", callback_data=WarehouseCallback(action="cs"))
    builder.button(text="🏠 Главное меню", callback_data=MainCallback(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZONE)


async def _guard(callback: CallbackQuery, auth: AuthService,
                 service: WarehouseAttendanceService | None, enabled: bool):
    if not enabled or service is None:
        await callback.answer("Складской модуль выключен.", show_alert=True); return None
    try:
        context = await asyncio.to_thread(auth.get_context, callback.from_user.id)
        if not context.can_use_warehouse_attendance:
            raise AccessDeniedError
        return context
    except AccessDeniedError:
        await callback.answer("Недостаточно прав.", show_alert=True); return None


@router.callback_query(WarehouseCallback.filter(F.action == "ao"))
async def open_prompt(callback: CallbackQuery, callback_data: WarehouseCallback,
                      auth_service: AuthService, warehouse_enabled: bool = False,
                      warehouse_attendance_service: WarehouseAttendanceService | None = None) -> None:
    context = await _guard(callback, auth_service, warehouse_attendance_service, warehouse_enabled)
    if context is None or not isinstance(callback.message, Message): return
    existing = await asyncio.to_thread(warehouse_attendance_service.status, callback.from_user.id)
    if existing is not None:
        await callback.answer("Смена уже открыта.", show_alert=True); return
    settings = await asyncio.to_thread(warehouse_attendance_service.repository.runtime_settings)
    now = warehouse_attendance_service.clock.now().astimezone(ZONE)
    await callback.answer()
    await edit_or_send_text(callback.message,
        f"Открыть рабочую смену?\n\nДата: {now:%d.%m.%Y}\nМесто: {escape(str(settings['source_location_name']))}\nВремя: {now:%H:%M}",
        attendance_confirm_keyboard("aO"))


@router.callback_query(WarehouseCallback.filter(F.action == "aO"))
async def open_confirm(callback: CallbackQuery, callback_data: WarehouseCallback,
                       auth_service: AuthService, warehouse_enabled: bool = False,
                       warehouse_attendance_service: WarehouseAttendanceService | None = None) -> None:
    context = await _guard(callback, auth_service, warehouse_attendance_service, warehouse_enabled)
    if context is None or not isinstance(callback.message, Message): return
    try:
        session, created = await asyncio.to_thread(warehouse_attendance_service.open, callback.from_user.id)
    except WarehouseAttendanceError:
        await callback.answer("Складская смена уже закрыта.", show_alert=True); return
    people = await asyncio.to_thread(warehouse_attendance_service.active_participants,
                                     int(session["warehouse_shift_id"]), callback.from_user.id)
    names = "\n".join(f"• {escape(str(x['display_name_snapshot']))}" for x in people)
    await callback.answer("Смена открыта." if created else "Смена уже открыта.")
    await edit_or_send_text(callback.message,
        f"Смена открыта в {_local(session['opened_at_utc']):%H:%M}.\n\nСейчас работают:\n{names}",
        _main_menu_for(context, True, True))


@router.callback_query(WarehouseCallback.filter(F.action == "ac"))
async def close_prompt(callback: CallbackQuery, callback_data: WarehouseCallback,
                       auth_service: AuthService, warehouse_enabled: bool = False,
                       warehouse_attendance_service: WarehouseAttendanceService | None = None) -> None:
    context = await _guard(callback, auth_service, warehouse_attendance_service, warehouse_enabled)
    if context is None or not isinstance(callback.message, Message): return
    session = await asyncio.to_thread(warehouse_attendance_service.status, callback.from_user.id)
    if session is None:
        await callback.answer("Смена уже закрыта.", show_alert=True); return
    seconds = warehouse_attendance_service.duration_seconds(session, warehouse_attendance_service.clock.now())
    await callback.answer()
    await edit_or_send_text(callback.message,
        f"Закрыть вашу рабочую смену?\n\nНачало: {_local(session['opened_at_utc']):%H:%M}\nСейчас: {warehouse_attendance_service.clock.now().astimezone(ZONE):%H:%M}\nПродолжительность: {seconds//3600} ч {(seconds%3600)//60:02d} мин",
        attendance_confirm_keyboard("aC"))


@router.callback_query(WarehouseCallback.filter(F.action == "aC"))
async def close_confirm(callback: CallbackQuery, callback_data: WarehouseCallback,
                        auth_service: AuthService, warehouse_enabled: bool = False,
                        warehouse_attendance_service: WarehouseAttendanceService | None = None) -> None:
    context = await _guard(callback, auth_service, warehouse_attendance_service, warehouse_enabled)
    if context is None or not isinstance(callback.message, Message): return
    session, changed = await asyncio.to_thread(warehouse_attendance_service.close, callback.from_user.id)
    if not changed or session is None:
        await callback.answer("Смена уже закрыта.", show_alert=True); return
    people = await asyncio.to_thread(warehouse_attendance_service.active_participants,
                                     int(session["warehouse_shift_id"]), callback.from_user.id)
    await callback.answer("Смена закрыта.")
    if people:
        names="\n".join(f"• {escape(str(x['display_name_snapshot']))}" for x in people)
        text=f"Ваша смена закрыта.\n\nПродолжают работать:\n{names}"
        keyboard=_main_menu_for(context, True, False)
    else:
        text="Вы последний участник смены.\n\nСкладская смена ещё не закрыта.\nПроверить обязательные списания и перейти к закрытию?"
        keyboard=last_participant_keyboard()
    await edit_or_send_text(callback.message,text,keyboard)


async def _send_report(callback: CallbackQuery, service: WarehouseAttendanceXlsxService,
                       period: str | date) -> None:
    try:
        path = await asyncio.to_thread(
            service.build_day if isinstance(period, date) else service.build_month,
            callback.from_user.id, period,
        )
        if isinstance(callback.message, Message):
            await callback.message.answer_document(FSInputFile(path), caption="📊 Табель рабочих смен")
    finally:
        if 'path' in locals(): path.unlink(missing_ok=True)


@router.callback_query(WarehouseCallback.filter(F.action == "tx"))
async def timesheet_menu(callback: CallbackQuery, callback_data: WarehouseCallback,
                         auth_service: AuthService,
                         warehouse_attendance_xlsx_service: WarehouseAttendanceXlsxService | None = None,
                         warehouse_enabled: bool = False) -> None:
    if not warehouse_enabled or warehouse_attendance_xlsx_service is None:
        await callback.answer("Складской модуль выключен.", show_alert=True); return
    context=await asyncio.to_thread(auth_service.get_context,callback.from_user.id)
    if not context.can_manage_warehouse_attendance:
        await callback.answer("Недостаточно прав.",show_alert=True); return
    await callback.answer()
    if isinstance(callback.message,Message):
        await edit_or_send_text(callback.message,"Выберите период табеля:",timesheet_period_keyboard())


@router.callback_query(WarehouseCallback.filter(F.action.in_({"td","tm","tp","ti"})))
async def timesheet_period(callback: CallbackQuery, callback_data: WarehouseCallback,
                           state: FSMContext, auth_service: AuthService,
                           warehouse_attendance_xlsx_service: WarehouseAttendanceXlsxService | None = None,
                           warehouse_enabled: bool = False) -> None:
    if not warehouse_enabled or warehouse_attendance_xlsx_service is None:
        await callback.answer("Складской модуль выключен.",show_alert=True); return
    context=await asyncio.to_thread(auth_service.get_context,callback.from_user.id)
    if not context.can_manage_warehouse_attendance:
        await callback.answer("Недостаточно прав.",show_alert=True); return
    today=datetime.now(ZONE).date()
    if callback_data.action=="ti":
        await state.set_state(AttendanceStates.report_month); await callback.answer()
        if isinstance(callback.message,Message): await callback.message.answer("Введите месяц в формате YYYY-MM")
        return
    if callback_data.action=="td": period: str|date=today
    else:
        target=today.replace(day=1)
        if callback_data.action=="tp": target=(target-timedelta(days=1)).replace(day=1)
        period=target.strftime("%Y-%m")
    await callback.answer("Формирую табель…")
    await _send_report(callback,warehouse_attendance_xlsx_service,period)


@router.message(AttendanceStates.report_month, F.text)
async def timesheet_manual(message: Message, state: FSMContext,
                           auth_service: AuthService,
                           warehouse_attendance_xlsx_service: WarehouseAttendanceXlsxService) -> None:
    if message.from_user is None: return
    context=await asyncio.to_thread(auth_service.get_context,message.from_user.id)
    if not context.can_manage_warehouse_attendance:
        await state.clear(); await message.answer("Недостаточно прав."); return
    value=(message.text or "").strip()
    try: parse_month(value)
    except ValueError as exc:
        await message.answer(escape(str(exc))); return
    path=None
    try:
        path=await asyncio.to_thread(warehouse_attendance_xlsx_service.build_month,message.from_user.id,value)
        await message.answer_document(FSInputFile(path),caption="📊 Табель рабочих смен")
    finally:
        if path is not None: path.unlink(missing_ok=True)
        await state.clear()


@router.callback_query(WarehouseCallback.filter(F.action == "af"))
async def force_list(callback: CallbackQuery, callback_data: WarehouseCallback,
                     auth_service: AuthService,
                     warehouse_attendance_service: WarehouseAttendanceService | None = None,
                     warehouse_enabled: bool = False) -> None:
    if not warehouse_enabled or warehouse_attendance_service is None:
        await callback.answer("Складской модуль выключен.",show_alert=True); return
    try: rows=await asyncio.to_thread(warehouse_attendance_service.open_sessions,callback.from_user.id)
    except AccessDeniedError:
        await callback.answer("Недостаточно прав.",show_alert=True); return
    await callback.answer()
    if not isinstance(callback.message,Message): return
    builder=InlineKeyboardBuilder()
    for row in rows:
        builder.button(text=f"{row['display_name_snapshot']} · {_local(row['opened_at_utc']):%d.%m %H:%M}",
                       callback_data=WarehouseCallback(action="ar",object_id=int(row["id"]),aux=int(row["revision"])))
    builder.button(text="⬅️ К складу",callback_data=WarehouseCallback(action="h")); builder.adjust(1)
    await edit_or_send_text(callback.message,"Незакрытые рабочие смены:" if rows else "Незакрытых рабочих смен нет.",builder.as_markup())


@router.callback_query(WarehouseCallback.filter(F.action == "ar"))
async def force_reason_prompt(callback: CallbackQuery, callback_data: WarehouseCallback,
                              state: FSMContext, auth_service: AuthService,
                              warehouse_attendance_service: WarehouseAttendanceService | None = None,
                              warehouse_enabled: bool = False) -> None:
    if not warehouse_enabled or warehouse_attendance_service is None:
        await callback.answer("Складской модуль выключен.",show_alert=True); return
    try: await asyncio.to_thread(warehouse_attendance_service.open_sessions,callback.from_user.id)
    except AccessDeniedError:
        await callback.answer("Недостаточно прав.",show_alert=True); return
    await state.set_state(AttendanceStates.force_reason)
    await state.update_data(session_id=callback_data.object_id,revision=callback_data.aux)
    await callback.answer()
    if isinstance(callback.message,Message): await callback.message.answer("Укажите обязательную причину принудительного закрытия.")


@router.message(AttendanceStates.force_reason, F.text)
async def force_reason_text(message: Message, state: FSMContext,
                            auth_service: AuthService) -> None:
    if message.from_user is None or (message.text or "").startswith("/"): return
    context=await asyncio.to_thread(auth_service.get_context,message.from_user.id)
    if not context.can_manage_warehouse_attendance:
        await state.clear(); await message.answer("Недостаточно прав."); return
    reason=(message.text or "").strip()
    if not reason or len(reason)>500:
        await message.answer("Причина должна содержать от 1 до 500 символов."); return
    data=await state.get_data(); await state.update_data(reason=reason)
    builder=InlineKeyboardBuilder(); builder.button(text="✅ Закрыть принудительно",callback_data=WarehouseCallback(action="aF",object_id=int(data["session_id"]),aux=int(data["revision"])))
    builder.button(text="❌ Отмена",callback_data=WarehouseCallback(action="af")); builder.adjust(1)
    await message.answer(f"Подтвердите принудительное закрытие.\n\nПричина: {escape(reason)}",reply_markup=builder.as_markup())


@router.callback_query(WarehouseCallback.filter(F.action == "aF"))
async def force_confirm(callback: CallbackQuery, callback_data: WarehouseCallback,
                        state: FSMContext, auth_service: AuthService,
                        warehouse_attendance_service: WarehouseAttendanceService | None = None,
                        warehouse_enabled: bool = False) -> None:
    if not warehouse_enabled or warehouse_attendance_service is None:
        await callback.answer("Складской модуль выключен.",show_alert=True); return
    data=await state.get_data(); reason=str(data.get("reason") or "")
    try:
        await asyncio.to_thread(warehouse_attendance_service.force_close,callback_data.object_id,
                                callback.from_user.id,reason,callback_data.aux)
    except (AccessDeniedError, WarehouseAttendanceError, ValueError) as exc:
        await callback.answer(str(exc) or "Запись уже изменена.",show_alert=True); return
    await state.clear(); await callback.answer("Рабочая смена закрыта принудительно.")
    if isinstance(callback.message,Message): await callback.message.answer("Сессия закрыта и записана в аудит.")
