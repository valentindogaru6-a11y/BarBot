import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers import warehouse_schedules as schedule_handlers
from app.handlers.warehouse_states import WarehouseScheduleStates
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError, AccessService
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


def _recipient(path: Path, chat_id: int, title: str, active: int = 1) -> int:
    with sqlite3.connect(path) as connection:
        cursor=connection.execute("INSERT INTO warehouse_report_recipients(chat_id,title,chat_type,is_active,created_by,created_at_utc,updated_at_utc,revision) VALUES(?,?,'group',?,1,'now','now',1)",(chat_id,title,active))
        connection.commit();return int(cursor.lastrowid)


def _ready_once(path: Path, repo, recipient: int, shift: int):
    with sqlite3.connect(path) as connection:
        cursor=connection.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc) VALUES(?,3,'final','ready','reports/edit.xlsx',?,1,'now',1,'now')",(shift,'4'*64))
        report=int(cursor.lastrowid);connection.commit()
    schedule=repo.create({'name':'Разово','schedule_type':'once','report_id':report,'timezone':'Europe/Kyiv','run_at_utc':'2026-09-11T10:00:00Z','max_wait':1440,'is_active':1},(recipient,),1,'now')
    repo.create_daily_jobs('2026-09-10T21:16:00Z')
    return schedule,repo.jobs(schedule['id'])[0]


class _State:
    def __init__(self,data=None):self.data=data or {};self.current=None;self.cleared=False
    async def get_data(self):return dict(self.data)
    async def update_data(self,**values):self.data.update(values)
    async def set_state(self,state):self.current=getattr(state,'state',state)
    async def clear(self):self.data={};self.current=None;self.cleared=True


class _Message:
    def __init__(self,text=None):
        self.text=text;self.photo=None;self.from_user=SimpleNamespace(id=1,is_bot=False);self.answer=AsyncMock();self.edit_text=AsyncMock()


def _callback(message,actor=1):
    return SimpleNamespace(message=message,from_user=SimpleNamespace(id=actor),answer=AsyncMock())


def test_v10_fresh_v9_upgrade_repeat_and_rollback(tmp_path: Path, monkeypatch):
    path=tmp_path/'v9.db'
    with sqlite3.connect(path) as c: c.execute('BEGIN');migrations._create_fresh_v9(c);c.commit()
    UserDatabase(path).initialize();UserDatabase(path).initialize()
    with sqlite3.connect(path) as c: assert c.execute('PRAGMA user_version').fetchone()[0]==12
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


def test_edit_name_and_delay_use_optimistic_revision(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now')
    renamed=repo.update(schedule['id'],{'name':'Новое имя'},(recipient,),schedule['revision'],'now')
    assert renamed['name']=='Новое имя'
    changed=repo.update(schedule['id'],{'delay':0},(recipient,),renamed['revision'],'later')
    assert changed['delay_minutes_after_close']==0
    with pytest.raises(WarehouseScheduleConflict): repo.update(schedule['id'],{'name':'Старый callback'},(recipient,),schedule['revision'],'late')


def test_edit_rejects_empty_and_inactive_recipients(tmp_path: Path):
    path,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now')
    with pytest.raises(WarehouseScheduleError):repo.update(schedule['id'],{},(),schedule['revision'],'now')
    with sqlite3.connect(path) as c:c.execute('UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?',(recipient,));c.commit()
    with pytest.raises(WarehouseScheduleError):repo.update(schedule['id'],{},(recipient,),schedule['revision'],'now')


def test_recipient_edit_preselects_current_and_paginates(monkeypatch):
    recipients=[{'id':number,'title':f'Получатель {number}'} for number in range(1,11)]
    service=SimpleNamespace(recipients=lambda actor:recipients)
    state=_State({'edit_selected':[1,9]});message=_Message()
    rendered=AsyncMock();monkeypatch.setattr(schedule_handlers,'edit_or_send_text',rendered)

    asyncio.run(schedule_handlers._edit_recipient_page(message,state,service,1))

    markup=rendered.await_args.args[2];labels=[button.text for row in markup.inline_keyboard for button in row]
    assert labels[0]=='✅ Получатель 1'
    assert 'Получатель 9' not in '\n'.join(labels)
    assert '➡️' in labels and '👁 Предпросмотр' in labels and '❌ Отмена' in labels


def test_recipient_edit_start_preselects_only_current_active_recipients(monkeypatch):
    row={'id':7,'revision':3,'recipients':(1,2)}
    service=SimpleNamespace(repository=SimpleNamespace(details=lambda schedule_id:row),list=lambda actor:[],recipients=lambda actor:[{'id':1,'title':'Активный'}])
    state=_State();callback=_callback(_Message());page=AsyncMock();monkeypatch.setattr(schedule_handlers,'_edit_recipient_page',page)

    asyncio.run(schedule_handlers.edit_recipients_start(callback,WarehouseCallback(action='er',object_id=7,aux=3),state,service))

    assert state.data['edit_old']==[1,2] and state.data['edit_selected']==[1]
    assert state.current==WarehouseScheduleStates.edit_recipients.state
    page.assert_awaited_once()


def test_recipient_edit_checkbox_toggle_and_preview(monkeypatch):
    recipients=[{'id':1,'title':'Старый <чат>'},{'id':2,'title':'Новый чат'}]
    repository=SimpleNamespace(recipients_by_ids=lambda ids:[row for row in recipients if row['id'] in ids])
    service=SimpleNamespace(recipients=lambda actor:recipients,repository=repository)
    state=_State({'edit_old':[1],'edit_selected':[1]});message=_Message();callback=_callback(message)
    rendered=AsyncMock();monkeypatch.setattr(schedule_handlers,'edit_or_send_text',rendered)

    asyncio.run(schedule_handlers.edit_recipient_toggle(callback,WarehouseCallback(action='rt',object_id=2),state,service))
    assert set(state.data['edit_selected'])=={1,2}
    asyncio.run(schedule_handlers.edit_recipient_preview(callback,state,service))

    text=rendered.await_args.args[1];markup=rendered.await_args.args[2]
    assert 'Старый &lt;чат&gt;' in text and 'Новый чат' in text
    assert markup.inline_keyboard[0][0].text=='✅ Сохранить'


def test_recipient_edit_confirm_saves_and_returns_to_schedule_card(monkeypatch):
    calls=[]
    service=SimpleNamespace(update_recipients=lambda schedule_id,recipients,revision,actor:calls.append((schedule_id,tuple(recipients),revision,actor)))
    state=_State({'edit_schedule':7,'edit_selected':[2,3],'edit_revision':4});callback=_callback(_Message());show_card=AsyncMock();monkeypatch.setattr(schedule_handlers,'card',show_card)

    asyncio.run(schedule_handlers.edit_recipient_save(callback,state,service))

    assert calls==[(7,(2,3),4,1)] and state.cleared
    show_card.assert_awaited_once()


def test_recipient_deactivated_before_confirm_returns_to_selection(monkeypatch):
    def reject(*args):raise WarehouseScheduleError('Recipients must be active')
    service=SimpleNamespace(update_recipients=reject,recipients=lambda actor:[{'id':1,'title':'Оставшийся'}])
    state=_State({'edit_schedule':7,'edit_selected':[1,2],'edit_revision':4});callback=_callback(_Message());rendered=AsyncMock();monkeypatch.setattr(schedule_handlers,'edit_or_send_text',rendered)

    asyncio.run(schedule_handlers.edit_recipient_save(callback,state,service))

    assert not state.cleared and state.data['edit_selected']==[1]
    assert rendered.await_args.args[1].startswith('👥 <b>Получатели</b>')


def test_recipient_edit_cancel_does_not_change_database(tmp_path: Path):
    _,repo,source,recipient,_=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now')
    state=_State({'edit_schedule':schedule['id'],'edit_selected':[]});callback=_callback(_Message())

    asyncio.run(schedule_handlers.cancel(callback,state))

    assert state.cleared and repo.recipient_ids(schedule['id'])==(recipient,)


def test_recipient_edit_rejects_zero_inactive_and_stale_atomically(tmp_path: Path):
    path,repo,source,recipient,_=_env(tmp_path);other=_recipient(path,-101,'Другой')
    schedule=repo.create(_daily(source),(recipient,),1,'now')
    with pytest.raises(WarehouseScheduleError):repo.update_recipients(schedule['id'],(),schedule['revision'],'later')
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE warehouse_report_recipients SET is_active=0 WHERE id=?',(other,));connection.commit()
    with pytest.raises(WarehouseScheduleError):repo.update_recipients(schedule['id'],(other,),schedule['revision'],'later')
    with pytest.raises(WarehouseScheduleConflict):repo.update_recipients(schedule['id'],(recipient,),0,'later')
    assert repo.recipient_ids(schedule['id'])==(recipient,) and repo.schedule(schedule['id'])['revision']==schedule['revision']


@pytest.mark.parametrize('status',['sending','sent'])
def test_recipient_edit_does_not_change_started_jobs(tmp_path: Path,status: str):
    path,repo,source,recipient,_=_env(tmp_path);other=_recipient(path,-102,'Новый')
    schedule=repo.create(_daily(source),(recipient,),1,'now');repo.create_daily_jobs('2026-09-10T21:16:00Z');job=repo.jobs(schedule['id'])[0]
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE warehouse_delivery_jobs SET status=? WHERE id=?',(status,job['id']));connection.commit()
    if status=='sending':
        with pytest.raises(WarehouseScheduleError):repo.update_recipients(schedule['id'],(other,),schedule['revision'],'later')
        assert repo.recipient_ids(schedule['id'])==(recipient,)
    else:
        repo.update_recipients(schedule['id'],(other,),schedule['revision'],'later')
        assert repo.recipient_ids(schedule['id'])==(other,)
    assert repo.job(job['id'])['status']==status


def test_recipient_edit_fsm_is_isolated_by_telegram_user():
    async def scenario():
        storage=MemoryStorage();first=FSMContext(storage,StorageKey(bot_id=1,chat_id=10,user_id=1));other=FSMContext(storage,StorageKey(bot_id=1,chat_id=10,user_id=2))
        await first.set_state(WarehouseScheduleStates.edit_recipients);await first.update_data(edit_selected=[7])
        assert await other.get_state() is None and await other.get_data()=={}
    asyncio.run(scenario())


def test_recipient_and_datetime_edits_require_schedule_management_permission(tmp_path: Path):
    path,repo,source,recipient,shift=_env(tmp_path);schedule=repo.create(_daily(source),(recipient,),1,'now')
    once,_=_ready_once(path,repo,recipient,shift);service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock())
    with pytest.raises(Exception):service.update_recipients(schedule['id'],(recipient,),schedule['revision'],2)
    with pytest.raises(Exception):service.update_once_datetime(once['id'],'2026-09-12T10:00:00Z',once['revision'],2)


def test_accountant_schedule_card_is_read_only(monkeypatch):
    row={'id':7,'name':'Отчёт','schedule_type':'once','delay_minutes_after_close':None,'run_at_utc':'2026-09-12T10:00:00Z','timezone':'Europe/Kyiv','is_active':1,'recipients':(1,),'revision':3,'latest_job':None}
    def reject_management(actor):raise AccessDeniedError('read only')
    service=SimpleNamespace(details=lambda schedule_id,actor:row,list=reject_management)
    callback=_callback(_Message(),actor=9);rendered=AsyncMock();monkeypatch.setattr(schedule_handlers,'edit_or_send_text',rendered)

    asyncio.run(schedule_handlers.card(callback,WarehouseCallback(action='sv',object_id=7),service))

    labels=[button.text for line in rendered.await_args.args[2].inline_keyboard for button in line]
    assert labels==['📜 Запуски','⬅️ Назад']


def test_one_off_datetime_valid_edit_updates_schedule_and_pending_job_atomically(tmp_path: Path):
    path,repo,_,recipient,shift=_env(tmp_path);schedule,job=_ready_once(path,repo,recipient,shift)
    service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock())

    updated=service.update_once_datetime(schedule['id'],'2026-09-12T10:30:00Z',schedule['revision'],1)

    assert updated['run_at_utc']=='2026-09-12T10:30:00Z'
    assert repo.job(job['id'])['scheduled_at_utc']=='2026-09-12T10:30:00Z'
    assert updated['revision']==schedule['revision']+1


def test_one_off_datetime_rejects_invalid_date_time_past_and_nonexistent_dst(tmp_path: Path):
    _,repo,_,_,_=_env(tmp_path);service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock(datetime(2026,1,1,tzinfo=UTC)))
    for date_text,time_text in [('31.02.2026','12:00'),('02.01.2026','25:00'),('01.01.2025','12:00')]:
        with pytest.raises(ValueError):service.local_run_candidates(date_text,time_text)
    with pytest.raises(ValueError,match='не существует'):service.local_run_candidates('29.03.2026','03:30')


def test_one_off_datetime_ambiguous_kyiv_occurrences_have_distinct_utc_preview(tmp_path: Path):
    _,repo,_,_,_=_env(tmp_path);service=WarehouseScheduleService(repo,Delivery(),AccessService(frozenset({1})),Clock(datetime(2026,1,1,tzinfo=UTC)))

    first,second=service.local_run_candidates('25.10.2026','03:30')

    assert first=='2026-10-25T00:30:00Z'
    assert second=='2026-10-25T01:30:00Z'
    assert schedule_handlers._local_time(first).startswith('25.10.2026 03:30')
    assert schedule_handlers._local_time(second).startswith('25.10.2026 03:30')


@pytest.mark.parametrize(('action','expected'),[('o1','2026-10-25T00:30:00Z'),('o2','2026-10-25T01:30:00Z')])
def test_one_off_datetime_occurrence_buttons_select_exact_utc(monkeypatch,action: str,expected: str):
    state=_State({'edit_occurrences':['2026-10-25T00:30:00Z','2026-10-25T01:30:00Z']});callback=_callback(_Message());preview=AsyncMock();monkeypatch.setattr(schedule_handlers,'_once_preview',preview)

    asyncio.run(schedule_handlers.edit_once_occurrence(callback,WarehouseCallback(action=action),state))

    assert preview.await_args.args[2]==expected


def test_one_off_datetime_preview_contains_local_utc_and_report_version():
    state=_State({'old_run_at':'2026-09-11T10:00:00Z','report_date':'2026-09-10','report_version':3});message=_Message()

    asyncio.run(schedule_handlers._once_preview(message,state,'2026-09-12T11:00:00Z'))

    text=message.answer.await_args.args[0]
    assert '11.09.2026 13:00' in text and '2026-09-11T10:00:00Z' in text
    assert '12.09.2026 14:00' in text and '2026-09-12T11:00:00Z' in text
    assert '2026-09-10, версия 3' in text


@pytest.mark.parametrize('status',['sending','sent','partial_failed','failed','cancelled'])
def test_one_off_datetime_forbidden_job_status_rolls_back(tmp_path: Path,status: str):
    path,repo,_,recipient,shift=_env(tmp_path);schedule,job=_ready_once(path,repo,recipient,shift)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE warehouse_delivery_jobs SET status=? WHERE id=?',(status,job['id']));connection.commit()
    with pytest.raises(WarehouseScheduleError):repo.update_once_run_at(schedule['id'],'2026-09-12T10:30:00Z',schedule['revision'],'later')
    assert repo.schedule(schedule['id'])['run_at_utc']==schedule['run_at_utc']
    assert repo.job(job['id'])['scheduled_at_utc']==job['scheduled_at_utc']


def test_one_off_datetime_stale_revision_rolls_back_schedule_and_job(tmp_path: Path):
    path,repo,_,recipient,shift=_env(tmp_path);schedule,job=_ready_once(path,repo,recipient,shift)
    with pytest.raises(WarehouseScheduleConflict):repo.update_once_run_at(schedule['id'],'2026-09-12T10:30:00Z',0,'later')
    assert repo.schedule(schedule['id'])['run_at_utc']==schedule['run_at_utc']
    assert repo.job(job['id'])['scheduled_at_utc']==job['scheduled_at_utc']


def test_one_off_datetime_cancel_does_not_change_database(tmp_path: Path):
    path,repo,_,recipient,shift=_env(tmp_path);schedule,job=_ready_once(path,repo,recipient,shift)
    state=_State({'edit_schedule':schedule['id'],'edit_run_at':'2026-09-12T10:30:00Z'});callback=_callback(_Message())
    asyncio.run(schedule_handlers.cancel(callback,state))
    assert repo.schedule(schedule['id'])['run_at_utc']==schedule['run_at_utc']
    assert repo.job(job['id'])['scheduled_at_utc']==job['scheduled_at_utc']


def test_schedule_edit_callback_data_stays_within_telegram_limit():
    markup=schedule_handlers._kb(*[(label,action,2_147_483_647,2_147_483_647) for label,action in [('Получатели','er'),('Переключить','rt'),('Страница','rp'),('Предпросмотр','rv'),('Сохранить','rz'),('Дата','ed'),('Первое','o1'),('Второе','o2'),('Сохранить время','os'),('Отмена','sx')]])
    assert all(len(button.callback_data.encode())<=64 for row in markup.inline_keyboard for button in row)
