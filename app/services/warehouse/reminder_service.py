"""Clock-injectable reminder calculation and Telegram orchestration."""
from __future__ import annotations
import asyncio
from datetime import UTC,datetime,timedelta
from zoneinfo import ZoneInfo
from app.services.warehouse.business_date_service import utc_rfc3339,Clock,SystemClock
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase
from app.services.access_service import AccessService
from app.keyboards.warehouse import reminder_notification_keyboard
class WarehouseReminderService:
 def __init__(self,repo,sender,clock:Clock|None=None):self.repository=repo;self.sender=sender;self.clock=clock or SystemClock()
 def defaults(self,source):return {'source_location_id':source,'enabled':0,'target_chat_id':None,'target_title':None,'timezone':'Europe/Kyiv','first_reminder_minutes_before_close':30,'repeat_interval_minutes':30,'max_reminders':3,'revision':0}
 def schedules(self,shift,settings):
  close=datetime.fromisoformat(shift['scheduled_close_at_utc'].replace('Z','+00:00')).astimezone(UTC)
  return [(1,utc_rfc3339(close-timedelta(minutes=int(settings['first_reminder_minutes_before_close']))))]+[(n,utc_rfc3339(close+timedelta(minutes=int(settings['repeat_interval_minutes']))*(n-2))) for n in range(2,int(settings['max_reminders'])+1)]
 async def run_once(self,now_utc=None):
  now=(now_utc or self.clock.now()).astimezone(UTC);stamp=utc_rfc3339(now)
  for shift in await asyncio.to_thread(self.repository.open_shifts): await asyncio.to_thread(self.repository.ensure_events,int(shift['id']),self.schedules(shift,shift),stamp)
  await asyncio.to_thread(self.repository.cancel_ineligible,stamp)
  rows=await asyncio.to_thread(self.repository.due,stamp,utc_rfc3339(now-timedelta(minutes=15)))
  for row in rows:
   claim=await asyncio.to_thread(self.repository.claim,int(row['id']),stamp)
   if not claim:continue
   try:mid=await self.sender.send_reminder(chat_id=int(claim['target_chat_id']),text=f"⏰ Смена за {row['business_date']} ещё не закрыта.\n\nПроверьте записи и обязательные списания.",reply_markup=reminder_notification_keyboard())
   except Exception as exc:await asyncio.to_thread(self.repository.finish,int(claim['id']),None,stamp,(type(exc).__name__.casefold(),'Telegram не принял напоминание'))
   else:await asyncio.to_thread(self.repository.finish,int(claim['id']),mid,stamp)
 async def run_forever(self,interval=45):
  while True: await self.run_once();await asyncio.sleep(interval)

class WarehouseReminderSettingsService:
 def __init__(self,repo,access,clock=None):self.repository=repo;self.access=access;self.clock=clock or SystemClock()
 def get(self,source,actor_user_id):
  self.access.require_warehouse_reminder_management(actor_user_id)
  return self.repository.settings(source) or WarehouseReminderService(self.repository,None,self.clock).defaults(source)
 def save(self,source,values,expected_revision,actor_user_id):
  self.access.require_warehouse_reminder_management(actor_user_id)
  from zoneinfo import ZoneInfo
  ZoneInfo(values['timezone'])
  if values['enabled'] and (values['target_chat_id'] is None or not values['target_title']):raise ValueError('Для включения нужен рабочий чат')
  return self.repository.save_settings(source,values,expected_revision,utc_rfc3339(self.clock.now().astimezone(UTC)))
