"""Read-only starting screen for v10 schedule administration."""
from __future__ import annotations
import asyncio
from aiogram import F,Router
from aiogram.types import CallbackQuery,Message
from app.handlers.common import edit_or_send_text
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.warehouse.schedule_service import WarehouseScheduleService
router=Router(name=__name__)
@router.callback_query(WarehouseCallback.filter(F.action=='sd'))
async def schedules(callback:CallbackQuery,warehouse_schedule_service:WarehouseScheduleService):
 try:rows=await asyncio.to_thread(warehouse_schedule_service.list,callback.from_user.id)
 except Exception:
  await callback.answer('Недостаточно прав.',show_alert=True);return
 await callback.answer()
 if isinstance(callback.message,Message):
  text='🗓 <b>Автоотправка отчётов</b>\n\n'+('\n'.join(f"• {row['name']} — {'вкл.' if row['is_active'] else 'выкл.'}" for row in rows) if rows else 'Расписаний ещё нет.')
  await edit_or_send_text(callback.message,text)
