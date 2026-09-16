"""v10 scheduled report delivery worker; it never closes or generates reports."""
from __future__ import annotations
import asyncio
import logging
from datetime import UTC,datetime,timedelta
from zoneinfo import ZoneInfo
from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import Clock,SystemClock,utc_rfc3339
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase
logger=logging.getLogger(__name__)
class WarehouseScheduleService:
 def __init__(self,repository:WarehouseScheduleDatabase,delivery:WarehouseReportDeliveryService,access:AccessService,clock:Clock|None=None):self.repository=repository;self.delivery=delivery;self.access=access;self.clock=clock or SystemClock()
 def _now(self,now=None):
  value=now or self.clock.now()
  if value.tzinfo is None or value.utcoffset() is None:raise ValueError('Clock must be timezone-aware')
  return value.astimezone(UTC)
 def create(self,values,recipient_ids,actor):
  self.access.require_warehouse_delivery_schedule_management(actor);ZoneInfo(values['timezone']);return self.repository.create(values,tuple(recipient_ids),actor,utc_rfc3339(self._now()))
 def update(self,id,values,recipient_ids,revision,actor):
  self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.update(id,values,tuple(recipient_ids),revision,utc_rfc3339(self._now()))
 def list(self,actor):self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.schedules()
 def details(self,id,actor):self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.details(id)
 def recipients(self,actor):self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.active_recipients()
 def reports(self,actor):self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.ready_reports()
 def sources(self,actor):self.access.require_warehouse_delivery_schedule_management(actor);return self.repository.sources()
 def local_run_at(self,date_text,time_text,timezone='Europe/Kyiv'):
  from datetime import datetime
  try:naive=datetime.strptime(f'{date_text} {time_text}','%d.%m.%Y %H:%M')
  except ValueError as exc:raise ValueError('Введите дату ДД.ММ.ГГГГ и время ЧЧ:ММ') from exc
  zone=ZoneInfo(timezone);first=naive.replace(tzinfo=zone,fold=0);second=naive.replace(tzinfo=zone,fold=1)
  def valid(value):return value.astimezone(UTC).astimezone(zone).replace(tzinfo=None)==naive
  if not valid(first):raise ValueError('Такого локального времени не существует')
  if first.utcoffset()!=second.utcoffset():raise ValueError('Неоднозначное локальное время: выберите occurrence')
  if first.astimezone(UTC)<=self._now():raise ValueError('Время отправки должно быть в будущем')
  return utc_rfc3339(first.astimezone(UTC))
 def jobs(self,id,actor):self.access.require_warehouse_delivery_history(actor);return self.repository.jobs(id)
 async def run_once(self,now_utc:datetime|None=None):
  now=self._now(now_utc);stamp=utc_rfc3339(now)
  await asyncio.to_thread(self.repository.create_daily_jobs,stamp)
  for job in await asyncio.to_thread(self.repository.due,stamp,utc_rfc3339(now-timedelta(minutes=15))):
   if not job['is_active']:continue
   report=await asyncio.to_thread(self.repository.report_for_job,job)
   timeout=datetime.fromisoformat(job['scheduled_at_utc'].replace('Z','+00:00')).astimezone(UTC)+timedelta(minutes=int(job['max_wait_minutes']))
   if report is None:
    await asyncio.to_thread(self.repository.wait_or_timeout,int(job['id']),stamp,utc_rfc3339(timeout));continue
   claim=await asyncio.to_thread(self.repository.claim,int(job['id']),int(report['id']),stamp)
   if claim is None:continue
   recipients=await asyncio.to_thread(self.repository.recipient_ids,int(job['schedule_id']),True)
   try:result=await self.delivery.deliver_scheduled(int(report['id']),recipient_ids=recipients,retry_failed=True)
   except Exception:
    logger.exception('Scheduled report delivery failed');await asyncio.to_thread(self.repository.finish,int(job['id']),'failed',stamp,'delivery_error');continue
   status='sent' if result['failed']==0 and result['pending']==0 else 'partial_failed' if result['delivered'] else 'failed'
   await asyncio.to_thread(self.repository.finish,int(job['id']),status,stamp,None if status=='sent' else 'delivery_failed')
   if job['schedule_type']=='once' and status=='sent':
    schedule=await asyncio.to_thread(self.repository.schedule,int(job['schedule_id']))
    if schedule:await asyncio.to_thread(self.repository.update,int(schedule['id']),{'is_active':0},await asyncio.to_thread(self.repository.recipient_ids,int(schedule['id'])),int(schedule['revision']),stamp)
 async def run_forever(self,interval=45):
  while True:
   try:await self.run_once()
   except asyncio.CancelledError:raise
   except Exception:logger.exception('Scheduled delivery tick failed')
   await asyncio.sleep(interval)
