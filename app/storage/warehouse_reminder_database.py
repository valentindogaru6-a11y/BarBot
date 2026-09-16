"""SQLite state for reminder settings and idempotent event claims."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

class WarehouseReminderError(RuntimeError): pass
class WarehouseReminderConflict(WarehouseReminderError): pass
class WarehouseReminderDatabase:
 def __init__(self,path): self.path=Path(path)
 @contextmanager
 def _c(self):
  c=sqlite3.connect(self.path,timeout=5,isolation_level=None); c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
  try: yield c
  finally: c.close()
 @contextmanager
 def _tx(self):
  with self._c() as c:
   try: c.execute('BEGIN IMMEDIATE');yield c;c.commit()
   except Exception:
    if c.in_transaction:c.rollback()
    raise
 @staticmethod
 def _d(r): return dict(r) if r else None
 def settings(self,source):
  with self._c() as c:return self._d(c.execute('SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?',(source,)).fetchone())
 def save_settings(self,source,values,expected_revision,now):
  with self._tx() as c:
   old=self._d(c.execute('SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?',(source,)).fetchone())
   if old is None:
    if expected_revision not in (0,None): raise WarehouseReminderConflict('Settings revision conflict')
    c.execute('INSERT INTO warehouse_reminder_settings(source_location_id,enabled,target_chat_id,target_title,timezone,first_reminder_minutes_before_close,repeat_interval_minutes,max_reminders,revision,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,1,?,?)',(source,values['enabled'],values['target_chat_id'],values['target_title'],values['timezone'],values['first'],values['repeat'],values['maximum'],now,now))
   else:
    if expected_revision != old['revision']: raise WarehouseReminderConflict('Settings revision conflict')
    cur=c.execute('UPDATE warehouse_reminder_settings SET enabled=?,target_chat_id=?,target_title=?,timezone=?,first_reminder_minutes_before_close=?,repeat_interval_minutes=?,max_reminders=?,revision=revision+1,updated_at_utc=? WHERE source_location_id=? AND revision=?',(values['enabled'],values['target_chat_id'],values['target_title'],values['timezone'],values['first'],values['repeat'],values['maximum'],now,source,expected_revision))
    if cur.rowcount!=1: raise WarehouseReminderConflict('Settings revision conflict')
   return self._d(c.execute('SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?',(source,)).fetchone())
 def open_shifts(self):
  with self._c() as c:return [dict(r) for r in c.execute("SELECT s.*,x.* FROM warehouse_shifts s JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id WHERE x.enabled=1 AND s.status IN ('open','waiting_required')").fetchall()]
 def ensure_events(self,shift_id,schedules,now):
  with self._tx() as c:
   for seq,when in schedules:c.execute("INSERT OR IGNORE INTO warehouse_reminder_events(shift_id,sequence_number,scheduled_at_utc,status,attempt_count,created_at_utc,updated_at_utc) VALUES (?,?,'{}','pending',0,?,?)".format(when),(shift_id,seq,now,now))
 def cancel_ineligible(self,now):
  with self._tx() as c:c.execute("UPDATE warehouse_reminder_events SET status='cancelled',updated_at_utc=? WHERE status IN ('pending','failed') AND shift_id IN (SELECT id FROM warehouse_shifts WHERE status IN ('closing','closed'))",(now,))
 def due(self,now,stale):
  with self._tx() as c:
   c.execute("UPDATE warehouse_reminder_events SET status='failed',last_error_code='stale_sending',last_error_text='Предыдущая отправка не завершена',updated_at_utc=? WHERE status='sending' AND claimed_at_utc<?",(now,stale))
   return [dict(r) for r in c.execute("SELECT e.*,s.status shift_status,s.business_date,x.enabled,x.target_chat_id,x.target_title FROM warehouse_reminder_events e JOIN warehouse_shifts s ON s.id=e.shift_id JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id WHERE e.status IN ('pending','failed') AND e.scheduled_at_utc<=?",(now,)).fetchall()]
 def claim(self,event_id,now):
  with self._tx() as c:
   row=c.execute("SELECT e.*,s.status shift_status,x.enabled,x.target_chat_id FROM warehouse_reminder_events e JOIN warehouse_shifts s ON s.id=e.shift_id JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id WHERE e.id=?",(event_id,)).fetchone()
   if not row:return None
   if row['shift_status'] not in ('open','waiting_required'):
    c.execute("UPDATE warehouse_reminder_events SET status='cancelled',updated_at_utc=? WHERE id=? AND status IN ('pending','failed')",(now,event_id));return None
   if not row['enabled'] or row['target_chat_id'] is None or row['status'] not in ('pending','failed'):return None
   if c.execute("UPDATE warehouse_reminder_events SET status='sending',attempt_count=attempt_count+1,last_error_code=NULL,last_error_text=NULL,claimed_at_utc=?,updated_at_utc=? WHERE id=? AND status=?",(now,now,event_id,row['status'])).rowcount!=1:return None
   return dict(c.execute('SELECT * FROM warehouse_reminder_events WHERE id=?',(event_id,)).fetchone())|{'target_chat_id':row['target_chat_id']}
 def finish(self,event_id,message_id,now,error=None):
  with self._tx() as c:
   if error is None:c.execute("UPDATE warehouse_reminder_events SET status='sent',telegram_message_id=?,sent_at_utc=?,updated_at_utc=? WHERE id=? AND status='sending'",(message_id,now,now,event_id))
   else:c.execute("UPDATE warehouse_reminder_events SET status='failed',last_error_code=?,last_error_text=?,updated_at_utc=? WHERE id=? AND status='sending'",(error[0][:100],error[1][:500],now,event_id))
 def events(self,source,limit=12):
  with self._c() as c:return [dict(r) for r in c.execute('SELECT e.* FROM warehouse_reminder_events e JOIN warehouse_shifts s ON s.id=e.shift_id WHERE s.source_location_id=? ORDER BY e.id DESC LIMIT ?',(source,limit)).fetchall()]
