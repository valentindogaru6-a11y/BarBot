import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.services.access_service import AccessService
from app.services.warehouse.reminder_service import WarehouseReminderService, WarehouseReminderSettingsService
from app.storage.user_database import UserDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase
from app.storage.warehouse_reminder_schema import validate_warehouse_reminder_schema

class Clock:
 def now(self): return datetime(2026,9,10,20,31,tzinfo=UTC)
class Sender:
 def __init__(self):self.calls=[]
 async def send_reminder(self,**kwargs):self.calls.append(kwargs);return 1

def test_v9_defaults_schedule_and_idempotent_tick(tmp_path:Path):
 path=tmp_path/'x.db';UserDatabase(path).initialize();repo=WarehouseReminderDatabase(path)
 with sqlite3.connect(path) as c:
  validate_warehouse_reminder_schema(c);source=c.execute("SELECT id FROM warehouse_locations WHERE stable_id='bar'").fetchone()[0]
  c.execute("INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,revision,created_at_utc,updated_at_utc) VALUES (?, '2026-09-10','Europe/Kyiv','23:00','open','2026-09-09T20:00:00Z','2026-09-10T21:00:00Z',1,'now','now')",(source,));c.commit()
 settings=WarehouseReminderSettingsService(repo,AccessService(frozenset({1})),Clock())
 assert settings.get(source,1)['enabled']==0
 settings.save(source,{'enabled':1,'target_chat_id':-100,'target_title':'Бар','timezone':'Europe/Kyiv','first':30,'repeat':30,'maximum':3},0,1)
 sender=Sender();service=WarehouseReminderService(repo,sender,Clock())
 asyncio.run(service.run_once());asyncio.run(service.run_once())
 assert len(sender.calls)==1
 assert [button.text for row in sender.calls[0]['reply_markup'].inline_keyboard for button in row]==['✅ Закрыть смену','📋 Записи за смену']
 with sqlite3.connect(path) as c:
  assert c.execute("SELECT sequence_number,scheduled_at_utc,status FROM warehouse_reminder_events ORDER BY sequence_number").fetchall()==[(1,'2026-09-10T20:30:00Z','sent'),(2,'2026-09-10T21:00:00Z','pending'),(3,'2026-09-10T21:30:00Z','pending')]
