"""SQLite persistence and atomic claims for v10 delivery schedules."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

class WarehouseScheduleConflict(RuntimeError): pass
class WarehouseScheduleError(RuntimeError): pass
class WarehouseScheduleDatabase:
 def __init__(self,path):self.path=Path(path)
 @contextmanager
 def _c(self):
  c=sqlite3.connect(self.path,timeout=5,isolation_level=None);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
  try:yield c
  finally:c.close()
 @contextmanager
 def _tx(self):
  with self._c() as c:
   try:c.execute('BEGIN IMMEDIATE');yield c;c.commit()
   except Exception:
    if c.in_transaction:c.rollback()
    raise
 def schedules(self):
  with self._c() as c:return [dict(r) for r in c.execute('SELECT * FROM warehouse_delivery_schedules ORDER BY is_active DESC,id DESC')]
 def schedule(self,id):
  with self._c() as c:
   r=c.execute('SELECT * FROM warehouse_delivery_schedules WHERE id=?',(id,)).fetchone();return dict(r) if r else None
 def active_recipients(self):
  with self._c() as c:return [dict(r) for r in c.execute("SELECT * FROM warehouse_report_recipients WHERE is_active=1 ORDER BY title COLLATE NOCASE,id")]
 def ready_reports(self):
  with self._c() as c:return [dict(r) for r in c.execute("SELECT wr.*,sh.business_date FROM warehouse_reports wr JOIN warehouse_shifts sh ON sh.id=wr.shift_id WHERE wr.status='ready' ORDER BY wr.id DESC LIMIT 30")]
 def sources(self):
  with self._c() as c:return [dict(r) for r in c.execute("SELECT * FROM warehouse_locations WHERE is_active=1 AND can_be_source=1 ORDER BY id")]
 def details(self,id):
  row=self.schedule(id)
  if row is None:return None
  row['recipients']=self.recipient_ids(id);row['latest_job']=self.jobs(id,1)[0] if self.jobs(id,1) else None
  return row
 def cancel_job(self,id,now):
  with self._tx() as c:return c.execute("UPDATE warehouse_delivery_jobs SET status='cancelled',finished_at_utc=?,updated_at_utc=? WHERE id=? AND shift_id IS NULL AND status IN ('scheduled','waiting_report')",(now,now,id)).rowcount==1
 def recipient_ids(self,schedule_id,active_only=False):
  with self._c() as c:return tuple(r[0] for r in c.execute('SELECT sr.recipient_id FROM warehouse_delivery_schedule_recipients sr JOIN warehouse_report_recipients r ON r.id=sr.recipient_id WHERE sr.schedule_id=?'+(' AND r.is_active=1' if active_only else ''),(schedule_id,)).fetchall())
 def create(self,values,recipient_ids,actor,now):
  if not recipient_ids:raise WarehouseScheduleError('Schedule requires recipients')
  with self._tx() as c:
   if c.execute('SELECT COUNT(*) FROM warehouse_report_recipients WHERE is_active=1 AND id IN ('+','.join('?'*len(recipient_ids))+')',recipient_ids).fetchone()[0]!=len(set(recipient_ids)):raise WarehouseScheduleError('Recipients must be active')
   if values['schedule_type']=='once' and c.execute("SELECT 1 FROM warehouse_reports WHERE id=? AND status='ready'",(values.get('report_id'),)).fetchone() is None:raise WarehouseScheduleError('One-off schedule requires a ready report')
   cur=c.execute('INSERT INTO warehouse_delivery_schedules(name,schedule_type,source_location_id,report_id,timezone,delay_minutes_after_close,run_at_utc,max_wait_minutes,is_active,created_by,revision,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)',(values['name'],values['schedule_type'],values.get('source_location_id'),values.get('report_id'),values['timezone'],values.get('delay'),values.get('run_at_utc'),values.get('max_wait',1440),values.get('is_active',0),actor,now,now));sid=cur.lastrowid
   c.executemany('INSERT INTO warehouse_delivery_schedule_recipients(schedule_id,recipient_id,created_at_utc) VALUES(?,?,?)',[(sid,r,now) for r in set(recipient_ids)])
   return self._load(c,sid)
 def update(self,id,values,recipient_ids,revision,now):
  if not recipient_ids:raise WarehouseScheduleError('Schedule requires recipients')
  with self._tx() as c:
   old=self._load(c,id)
   if not old:raise WarehouseScheduleError('Schedule not found')
   if old['revision']!=revision:raise WarehouseScheduleConflict('Schedule revision conflict')
   if values.get('is_active',old['is_active']) and c.execute('SELECT COUNT(*) FROM warehouse_report_recipients WHERE is_active=1 AND id IN ('+','.join('?'*len(recipient_ids))+')',recipient_ids).fetchone()[0]!=len(set(recipient_ids)):raise WarehouseScheduleError('Recipients must be active')
   c.execute('UPDATE warehouse_delivery_schedules SET name=?,delay_minutes_after_close=?,run_at_utc=?,is_active=?,revision=revision+1,updated_at_utc=? WHERE id=? AND revision=?',(values.get('name',old['name']),values.get('delay',old['delay_minutes_after_close']),values.get('run_at_utc',old['run_at_utc']),values.get('is_active',old['is_active']),now,id,revision))
   c.execute('DELETE FROM warehouse_delivery_schedule_recipients WHERE schedule_id=?',(id,));c.executemany('INSERT INTO warehouse_delivery_schedule_recipients(schedule_id,recipient_id,created_at_utc) VALUES(?,?,?)',[(id,r,now) for r in set(recipient_ids)])
   c.execute("UPDATE warehouse_delivery_jobs SET status='cancelled',updated_at_utc=? WHERE schedule_id=? AND status IN ('scheduled','waiting_report') AND ?=0",(now,id,values.get('is_active',old['is_active'])))
   return self._load(c,id)
 def _load(self,c,id):
  r=c.execute('SELECT * FROM warehouse_delivery_schedules WHERE id=?',(id,)).fetchone();return dict(r) if r else None
 def create_daily_jobs(self,now):
  with self._tx() as c:
   rows=c.execute("SELECT s.*,sh.id shift_id,sh.business_date,sh.scheduled_close_at_utc FROM warehouse_delivery_schedules s JOIN warehouse_shifts sh ON sh.source_location_id=s.source_location_id WHERE s.is_active=1 AND s.schedule_type='daily'").fetchall()
   for r in rows:
    scheduled=_plus(r['scheduled_close_at_utc'],int(r['delay_minutes_after_close']))
    c.execute("INSERT OR IGNORE INTO warehouse_delivery_jobs(schedule_id,shift_id,report_id,business_date,scheduled_at_utc,status,created_at_utc,updated_at_utc) VALUES(?,?,NULL,?,?, 'scheduled',?,?)",(r['id'],r['shift_id'],r['business_date'],scheduled,now,now))
   ones=c.execute("SELECT * FROM warehouse_delivery_schedules WHERE is_active=1 AND schedule_type='once'").fetchall()
   for r in ones:c.execute("INSERT OR IGNORE INTO warehouse_delivery_jobs(schedule_id,shift_id,report_id,business_date,scheduled_at_utc,status,created_at_utc,updated_at_utc) SELECT ?,NULL,?,sh.business_date,?,'scheduled',?,? FROM warehouse_reports wr JOIN warehouse_shifts sh ON sh.id=wr.shift_id WHERE wr.id=?",(r['id'],r['report_id'],r['run_at_utc'],now,now,r['report_id']))
 def due(self,now,stale):
  with self._tx() as c:
   c.execute("UPDATE warehouse_delivery_jobs SET status='failed',last_error_code='stale_sending',last_error_text='Предыдущая отправка не завершена',updated_at_utc=? WHERE status='sending' AND claimed_at_utc<?",(now,stale))
   return [dict(r) for r in c.execute("SELECT j.*,s.is_active,s.max_wait_minutes,s.schedule_type FROM warehouse_delivery_jobs j JOIN warehouse_delivery_schedules s ON s.id=j.schedule_id WHERE j.status IN ('scheduled','waiting_report','failed','partial_failed') AND j.scheduled_at_utc<=?",(now,)).fetchall()]
 def report_for_job(self,job):
  with self._c() as c:
   if job['report_id']:r=c.execute("SELECT * FROM warehouse_reports WHERE id=? AND status='ready'",(job['report_id'],)).fetchone()
   else:r=c.execute("SELECT * FROM warehouse_reports WHERE shift_id=? AND status='ready' ORDER BY id DESC LIMIT 1",(job['shift_id'],)).fetchone()
   return dict(r) if r else None
 def wait_or_timeout(self,id,now,timeout):
  with self._tx() as c:c.execute("UPDATE warehouse_delivery_jobs SET status=?,last_error_code=?,last_error_text=?,finished_at_utc=CASE WHEN ?>? THEN ? ELSE NULL END,updated_at_utc=? WHERE id=? AND status IN ('scheduled','waiting_report','failed','partial_failed')",('failed' if now>timeout else 'waiting_report','report_not_ready_timeout' if now>timeout else None,'Отчёт не готов за допустимое время' if now>timeout else None,now,timeout,now,now,id))
 def claim(self,id,report_id,now):
  with self._tx() as c:
   row=c.execute("SELECT j.*,s.is_active FROM warehouse_delivery_jobs j JOIN warehouse_delivery_schedules s ON s.id=j.schedule_id WHERE j.id=?",(id,)).fetchone()
   if not row or not row['is_active'] or row['status'] not in ('scheduled','waiting_report','failed','partial_failed'):return None
   cur=c.execute("UPDATE warehouse_delivery_jobs SET status='sending',report_id=?,claimed_at_utc=?,started_at_utc=COALESCE(started_at_utc,?),last_error_code=NULL,last_error_text=NULL,updated_at_utc=? WHERE id=? AND status=?",(report_id,now,now,now,id,row['status']))
   return dict(c.execute('SELECT * FROM warehouse_delivery_jobs WHERE id=?',(id,)).fetchone()) if cur.rowcount else None
 def finish(self,id,status,now,code=None):
  with self._tx() as c:c.execute("UPDATE warehouse_delivery_jobs SET status=?,finished_at_utc=?,last_error_code=?,last_error_text=?,updated_at_utc=? WHERE id=? AND status='sending'",(status,now,code,'Telegram доставка завершилась не полностью' if code else None,now,id))
 def jobs(self,schedule_id,limit=20):
  with self._c() as c:return [dict(r) for r in c.execute('SELECT * FROM warehouse_delivery_jobs WHERE schedule_id=? ORDER BY id DESC LIMIT ?',(schedule_id,limit)).fetchall()]
 def job(self,id):
  with self._c() as c:
   r=c.execute('SELECT * FROM warehouse_delivery_jobs WHERE id=?',(id,)).fetchone();return dict(r) if r else None
def _plus(stamp,minutes):
 from datetime import UTC,datetime,timedelta
 return (datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(UTC)+timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace('+00:00','Z')
