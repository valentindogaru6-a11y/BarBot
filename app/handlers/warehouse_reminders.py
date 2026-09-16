"""Minimal admin screen for warehouse reminder settings."""
from __future__ import annotations
import asyncio
from aiogram import F,Router
from aiogram.types import CallbackQuery,Message
from app.handlers.common import edit_or_send_text
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.keyboards.warehouse import warehouse_home_keyboard
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.operations_service import WarehouseOperationsService
router=Router(name=__name__)
@router.callback_query(WarehouseCallback.filter(F.action=='mr'))
async def reminders(callback:CallbackQuery,warehouse_reminder_settings_service:WarehouseReminderSettingsService,warehouse_service:WarehouseOperationsService):
 try:
  source=int((await asyncio.to_thread(warehouse_service.repository.runtime_settings))['source_location_id'])
  settings=await asyncio.to_thread(warehouse_reminder_settings_service.get,source,callback.from_user.id)
 except Exception:
  await callback.answer('Недостаточно прав.',show_alert=True);return
 await callback.answer()
 if isinstance(callback.message,Message):
  await edit_or_send_text(callback.message,f"🔔 <b>Напоминания</b>\n\nСтатус: {'включены' if settings['enabled'] else 'выключены'}\nРабочий чат: {settings['target_title'] or 'не выбран'}\nTimezone: {settings['timezone']}\nПервое напоминание: за {settings['first_reminder_minutes_before_close']} мин.\nИнтервал: {settings['repeat_interval_minutes']} мин.\nМаксимум повторов: {settings['max_reminders']}")
