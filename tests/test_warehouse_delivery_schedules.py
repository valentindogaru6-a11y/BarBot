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


def test_one_off_local_datetime_rejects_invalid_past_and_dst_cases(tmp_path: Path):
    _,repo,_,_,_=_env(tmp_path)
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock(datetime(2026,1,1,0,0,tzinfo=UTC)))
    with pytest.raises(ValueError): service.local_run_at('31.02.2026','12:00')
    with pytest.raises(ValueError): service.local_run_at('01.01.2025','12:00')
    with pytest.raises(ValueError,match='не существует'): service.local_run_at('29.03.2026','03:30')
    with pytest.raises(ValueError,match='Неоднозначное'): service.local_run_at('25.10.2026','03:30')


def test_one_off_local_datetime_has_explicit_utc_preview(tmp_path: Path):
    _,repo,_,_,_=_env(tmp_path)
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock(datetime(2026,1,1,0,0,tzinfo=UTC)))
    assert service.local_run_at('02.01.2026','12:00')=='2026-01-02T10:00:00Z'


def test_schedule_management_permissions_and_read_only_history(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path)
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock())
    with pytest.raises(Exception): service.create(_daily(source),(recipient,),2)
    with pytest.raises(Exception): service.jobs(1,2)


def test_disable_cancels_pending_jobs_and_enable_uses_revision(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now');repo.create_daily_jobs('2026-09-10T21:16:00Z')
    updated=repo.update(schedule['id'],{'is_active':0},(recipient,),schedule['revision'],'later')
    assert updated['is_active']==0 and repo.jobs(schedule['id'])[0]['status']=='cancelled'


def test_stale_job_is_recovered_as_failed(tmp_path: Path):
    path,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now');repo.create_daily_jobs('2026-09-10T21:16:00Z');job=repo.jobs(schedule['id'])[0]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE warehouse_delivery_jobs SET status='sending',claimed_at_utc='2026-09-10T20:00:00Z' WHERE id=?",(job['id'],));connection.commit()
    assert repo.due('2026-09-10T21:16:00Z','2026-09-10T21:01:00Z')[0]['status']=='failed'


def test_schedule_card_details_and_jobs_are_safe_for_history_reader(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now');repo.create_daily_jobs('2026-09-10T21:16:00Z')
    detail=repo.details(schedule['id'])
    assert detail['name']=='Ежедневно' and detail['recipients']==(recipient,) and detail['latest_job']['business_date']=='2026-09-10'


def test_cancel_only_pending_one_off_job_is_idempotent(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc) VALUES(?,1,'final','ready','reports/z.xlsx',?,1,'now',1,'now')",(shift,'2'*64));report=c.execute('SELECT id FROM warehouse_reports').fetchone()[0];c.commit()
    once=repo.create({'name':'Разово','schedule_type':'once','report_id':report,'timezone':'Europe/Kyiv','run_at_utc':'2026-09-10T21:30:00Z','max_wait':1440,'is_active':1},(recipient,),1,'now');repo.create_daily_jobs('now');job=repo.jobs(once['id'])[0]
    assert repo.cancel_job(job['id'],'now') is True and repo.cancel_job(job['id'],'now') is False


def test_retry_reuses_only_selected_active_recipients(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc) VALUES(?,1,'final','ready','reports/retry.xlsx',?,1,'now',1,'now')",(shift,'3'*64));report=c.execute('SELECT id FROM warehouse_reports').fetchone()[0];c.commit()
    once=repo.create({'name':'Retry','schedule_type':'once','report_id':report,'timezone':'Europe/Kyiv','run_at_utc':'2026-09-10T20:00:00Z','max_wait':1440,'is_active':1},(recipient,),1,'now');repo.create_daily_jobs('now');job=repo.jobs(once['id'])[0]
    with sqlite3.connect(path) as c:c.execute("UPDATE warehouse_delivery_jobs SET status='failed' WHERE id=?",(job['id'],));c.commit()
    delivery=Delivery();service=WarehouseScheduleService(repo,delivery,AccessService(frozenset({1})),Clock())
    assert asyncio.run(service.retry_job(job['id'],1))['delivered']==1 and delivery.calls[0][1]==(recipient,)
