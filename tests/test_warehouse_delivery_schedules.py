import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.access_service import AccessService
from app.services.warehouse.schedule_service import WarehouseScheduleService
from app.storage import migrations
from app.storage.user_database import UserDatabase
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase, WarehouseScheduleConflict, WarehouseScheduleError


class Clock:
    def __init__(self, value=datetime(2026, 9, 10, 21, 16, tzinfo=UTC)): self.value=value
    def now(self): return self.value


class Delivery:
    def __init__(self): self.calls=[]
    async def deliver_scheduled(self, report_id, *, recipient_ids, retry_failed=True):
        self.calls.append((report_id, recipient_ids, retry_failed))
        return {"delivered": len(recipient_ids), "failed": 0, "pending": 0}


def _env(tmp_path: Path):
    path=tmp_path/'schedule.db'; UserDatabase(path).initialize(); repo=WarehouseScheduleDatabase(path)
    with sqlite3.connect(path) as c:
        source=c.execute("SELECT id FROM warehouse_locations WHERE stable_id='bar'").fetchone()[0]
        c.execute("INSERT INTO warehouse_report_recipients(chat_id,title,chat_type,is_active,created_by,created_at_utc,updated_at_utc,revision) VALUES(-100,'Бар','group',1,1,'now','now',1)")
        recipient=c.execute('SELECT id FROM warehouse_report_recipients').fetchone()[0]
        c.execute("INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,revision,created_at_utc,updated_at_utc) VALUES(?, '2026-09-10','Europe/Kyiv','23:00','closed','now','2026-09-10T21:00:00Z',1,'now','now')",(source,))
        shift=c.execute('SELECT id FROM warehouse_shifts').fetchone()[0];c.commit()
    return path,repo,source,recipient,shift


def _daily(source): return {"name":"Ежедневно","schedule_type":"daily","source_location_id":source,"timezone":"Europe/Kyiv","delay":15,"max_wait":1440,"is_active":1}


def test_v10_fresh_v9_upgrade_repeat_and_rollback(tmp_path: Path, monkeypatch):
    path=tmp_path/'v9.db'
    with sqlite3.connect(path) as c: c.execute('BEGIN');migrations._create_fresh_v9(c);c.commit()
    UserDatabase(path).initialize();UserDatabase(path).initialize()
    with sqlite3.connect(path) as c: assert c.execute('PRAGMA user_version').fetchone()[0]==10
    failed=tmp_path/'failed.db'
    with sqlite3.connect(failed) as c: c.execute('BEGIN');migrations._create_fresh_v9(c);c.commit()
    monkeypatch.setattr(migrations,'create_warehouse_schedule_schema',lambda c: (_ for _ in ()).throw(RuntimeError('fail')))
    with pytest.raises(migrations.DatabaseMigrationError): UserDatabase(failed).initialize()
    with sqlite3.connect(failed) as c: assert c.execute('PRAGMA user_version').fetchone()[0]==9


def test_daily_delay_dst_no_fake_shift_recipients_and_revision(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path)
    with pytest.raises(WarehouseScheduleError): repo.create(_daily(source),(),1,'now')
    schedule=repo.create(_daily(source),(recipient,),1,'now')
    repo.create_daily_jobs('2026-09-10T21:16:00Z')
    job=repo.jobs(schedule['id'])[0]
    assert job['shift_id']==shift and job['scheduled_at_utc']=='2026-09-10T21:15:00Z'
    with pytest.raises(WarehouseScheduleConflict): repo.update(schedule['id'],{},(recipient,),0,'now')
    # Daily calculation is based on stored UTC close snapshots, so Kyiv summer/winter offsets are preserved.
    with sqlite3.connect(path) as c: assert c.execute('SELECT COUNT(*) FROM warehouse_delivery_jobs').fetchone()[0]==1


def test_waiting_report_timeout_and_ready_report_execution(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path); schedule=repo.create(_daily(source),(recipient,),1,'now')
    delivery=Delivery(); service=WarehouseScheduleService(repo,delivery,AccessService(frozenset({1})),Clock())
    asyncio.run(service.run_once())
    assert repo.jobs(schedule['id'])[0]['status']=='waiting_report'
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc) VALUES(?,1,'final','ready','reports/x.xlsx',?,1,'now',1,'now')",(shift,'0'*64));c.commit()
    asyncio.run(service.run_once())
    assert delivery.calls==[(1,(recipient,),True)] and repo.jobs(schedule['id'])[0]['status']=='sent'


def test_waiting_report_times_out_after_24_hours(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path); schedule=repo.create(_daily(source),(recipient,),1,'now')
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock(datetime(2026,9,11,21,16,tzinfo=UTC)))
    asyncio.run(service.run_once())
    assert repo.jobs(schedule['id'])[0]['last_error_code']=='report_not_ready_timeout'


def test_once_is_single_job_and_deactivated_recipient_is_skipped(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc) VALUES(?,1,'final','ready','reports/y.xlsx',?,1,'now',1,'now')",(shift,'1'*64));report=c.execute('SELECT id FROM warehouse_reports').fetchone()[0];c.commit()
    once=repo.create({"name":"Разово","schedule_type":"once","report_id":report,"timezone":"Europe/Kyiv","run_at_utc":"2026-09-10T21:15:00Z","max_wait":1440,"is_active":1},(recipient,),1,'now')
    with sqlite3.connect(path) as c:c.execute('UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?',(recipient,));c.commit()
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock())
    asyncio.run(service.run_once());asyncio.run(service.run_once())
    assert len(repo.jobs(once['id']))==1
