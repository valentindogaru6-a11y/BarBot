"""Phase 9A backend correction lifecycle on temporary databases and files."""

import asyncio
import shutil
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.seed_service import load_warehouse_seed
from app.services.warehouse.xlsx_report_service import WarehouseXlsxReportService
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_correction_database import (
    WarehouseCorrectionConflict,
    WarehouseCorrectionDatabase,
    WarehouseCorrectionError,
)
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsError
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT=Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):self.value=datetime(2026,9,10,19,0,tzinfo=UTC)
    def now(self):return self.value


@pytest.fixture(scope='module')
def correction_template(tmp_path_factory):
    path=tmp_path_factory.mktemp('warehouse-corrections')/'template.db';UserDatabase(path).initialize()
    WarehouseSeedDatabase(path).import_seed(load_warehouse_seed(ROOT/'source'/'warehouse_products_seed.json'),apply=True)
    return path


@pytest.fixture
def environment(correction_template,tmp_path):
    project=tmp_path/'project';project.mkdir();path=project/'warehouse.db';shutil.copyfile(correction_template,path)
    users=UserService(UserDatabase(path))
    for user_id in (10,20,30,40,50):users.ensure_user(user_id)
    users.set_role(10,Role.ADMIN,actor_user_id=1);users.set_role(20,Role.BARTENDER,actor_user_id=10);users.set_role(30,Role.ACCOUNTANT,actor_user_id=10);users.set_role(40,Role.USER,actor_user_id=10);users.set_role(50,Role.BLOCKED,actor_user_id=10)
    access=AuthService(frozenset({1}),users);clock=Clock();operations=WarehouseOperationsService(WarehouseOperationsDatabase(path),access,clock=clock)
    closing=WarehouseClosingService(WarehouseClosingDatabase(path),operations,access,clock=clock)
    xlsx=WarehouseXlsxReportService(project/'reports'/'warehouse',project_root=project)
    reports=WarehouseReportService(WarehouseReportsDatabase(path),operations,access,project/'reports'/'warehouse',clock=clock,xlsx_service=xlsx)
    corrections=WarehouseCorrectionService(WarehouseCorrectionDatabase(path),operations,access,xlsx,clock=clock)
    return {'path':path,'project':project,'users':users,'access':access,'clock':clock,'operations':operations,'closing':closing,'xlsx':xlsx,'reports':reports,'corrections':corrections}


def _product(env,name):return next(item for item in env['operations'].repository.products() if item.official_name==name)


def _finish_closing(env,actor=20):
    session=env['closing'].start(actor_user_id=actor)
    for requirement in session['requirements']:
        if requirement['status']=='pending':session=env['closing'].mark_not_applicable(session['id'],requirement['id'],'Остатков не было',actor_user_id=actor)
    session=env['closing'].resume(session['id'],actor_user_id=actor);env['closing'].finalize(session['id'],expected_revision=session['revision'],actor_user_id=actor)
    return session['shift_id']


def _initial_report(env):
    orange=_product(env,'Апельсин');env['operations'].create_manual_operation(actor_user_id=20,operation_type='writeoff',product_id=orange.id,quantity_text='0.15',reason_text='исходное')
    sugar=_product(env,'Сахар');destination=env['operations'].destination_options(actor_user_id=20)[0];env['operations'].create_manual_operation(actor_user_id=20,operation_type='transfer',product_id=sugar.id,quantity_text='0.2',destination_location_id=destination.id,comment_text='исходное')
    batch=env['operations'].prepare_batch(actor_user_id=20,source_chat_id=777,source_message_id=901,raw_text='проработка: Исходная проработка\nапельсин 0.1 кг');env['operations'].commit_batch(batch['id'],actor_user_id=20)
    shift_id=_finish_closing(env);return env['reports'].build_final(shift_id,actor_user_id=20)['report']


def _open(env,reason='Исправление остатков',actor=10):
    report=_initial_report(env);shift=env['operations'].repository.shift(report['shift_id'])
    session=env['corrections'].open_correction(report['shift_id'],reason,expected_shift_revision=shift['revision'],actor_user_id=actor)
    return report,session


def test_correction_open_admin_super_admin_and_read_only_roles(environment):
    report=_initial_report(environment);shift=environment['operations'].repository.shift(report['shift_id'])
    for actor in (20,30,40,50):
        with pytest.raises(AccessDeniedError):environment['corrections'].open_correction(report['shift_id'],'Причина',expected_shift_revision=shift['revision'],actor_user_id=actor)
    session=environment['corrections'].open_correction(report['shift_id'],'Причина',expected_shift_revision=shift['revision'],actor_user_id=1)
    assert session['status']=='open'
    assert environment['corrections'].history(report['shift_id'],actor_user_id=30)[0]['reason_text']=='Причина'
    with pytest.raises(AccessDeniedError):environment['corrections'].create_operation(session['id'],operation_type='writeoff',product_id=_product(environment,'Апельсин').id,quantity_text='0.1',actor_user_id=30)


def test_correction_open_requires_closed_shift_ready_report_and_valid_reason(environment):
    orange=_product(environment,'Апельсин');operation=environment['operations'].create_manual_operation(actor_user_id=20,operation_type='writeoff',product_id=orange.id,quantity_text='0.1');shift=environment['operations'].repository.shift(operation['shift_id'])
    with pytest.raises(WarehouseCorrectionError,match='closed'):environment['corrections'].open_correction(shift['id'],'Причина',expected_shift_revision=shift['revision'],actor_user_id=10)
    with sqlite3.connect(environment['path']) as connection:connection.execute("UPDATE warehouse_shifts SET status='closed' WHERE id=?",(shift['id'],));connection.commit()
    shift=environment['operations'].repository.shift(shift['id'])
    with pytest.raises(WarehouseCorrectionError,match='Ready'):environment['corrections'].open_correction(shift['id'],'Причина',expected_shift_revision=shift['revision'],actor_user_id=10)
    for reason in ('   ','x'*501):
        with pytest.raises(WarehouseCorrectionError):environment['corrections'].open_correction(shift['id'],reason,expected_shift_revision=shift['revision'],actor_user_id=10)


def test_correction_duplicate_and_concurrent_open_is_idempotent(environment):
    report=_initial_report(environment);shift=environment['operations'].repository.shift(report['shift_id'])
    def opening():return environment['corrections'].open_correction(report['shift_id'],'Одна причина',expected_shift_revision=shift['revision'],actor_user_id=10)
    with ThreadPoolExecutor(max_workers=2) as pool:first,second=list(pool.map(lambda _:opening(),range(2)))
    assert first['id']==second['id']
    with sqlite3.connect(environment['path']) as connection:assert connection.execute("SELECT COUNT(*) FROM warehouse_correction_sessions WHERE shift_id=?",(report['shift_id'],)).fetchone()[0]==1


def test_correction_open_uses_shift_cas_revision(environment):
    report=_initial_report(environment);shift=environment['operations'].repository.shift(report['shift_id'])
    with pytest.raises(WarehouseCorrectionConflict):environment['corrections'].open_correction(report['shift_id'],'Причина',expected_shift_revision=shift['revision']-1,actor_user_id=10)
    assert environment['operations'].repository.shift(report['shift_id'])['status']=='closed'


def test_correction_mutations_add_update_delete_restore_and_audit(environment):
    _,session=_open(environment);orange=_product(environment,'Апельсин')
    created=environment['corrections'].create_operation(session['id'],operation_type='writeoff',product_id=orange.id,quantity_text='0.2',reason_text='добавлено',actor_user_id=10)
    occurred=created['occurred_at_utc'];lime=_product(environment,'Лайм');updated=environment['corrections'].update_product_and_quantity(session['id'],created['id'],lime.id,'0.3','кг',expected_revision=created['revision'],actor_user_id=10)
    assert updated['product_id']==lime.id
    noted=environment['corrections'].update_note(session['id'],created['id'],reason_text='новая причина',comment_text=None,expected_revision=updated['revision'],actor_user_id=10)
    deleted=environment['corrections'].delete_operation(session['id'],created['id'],expected_revision=noted['revision'],reason='ошибка',actor_user_id=10)
    with pytest.raises(WarehouseCorrectionError,match='restored'):environment['corrections'].update_quantity(session['id'],created['id'],'0.4',None,expected_revision=deleted['revision'],actor_user_id=10)
    restored=environment['corrections'].restore_operation(session['id'],created['id'],expected_revision=deleted['revision'],reason='вернуть',actor_user_id=10)
    assert restored['is_deleted']==0 and restored['occurred_at_utc']==occurred
    with sqlite3.connect(environment['path']) as connection:
        rows=connection.execute("SELECT event_type,before_json,after_json,correlation_id FROM warehouse_audit_events WHERE correlation_id=? ORDER BY id",(f"correction:{session['id']}",)).fetchall()
    assert {'correction_operation_created','correction_operation_updated','correction_operation_deleted','correction_operation_restored'}<= {row[0] for row in rows}
    assert all(row[3]==f"correction:{session['id']}" for row in rows) and any(row[1] and row[2] for row in rows)


def test_correction_transfer_and_development_mutations(environment):
    _,session=_open(environment);sugar=_product(environment,'Сахар');destinations=environment['operations'].destination_options(actor_user_id=10)
    transfer=environment['corrections'].create_operation(session['id'],operation_type='transfer',product_id=sugar.id,quantity_text='0.2',destination_location_id=destinations[0].id,actor_user_id=10)
    moved=environment['corrections'].update_destination(session['id'],transfer['id'],destinations[1].id,expected_revision=transfer['revision'],actor_user_id=10);assert moved['destination_location_id']==destinations[1].id
    orange=_product(environment,'Апельсин');development=environment['corrections'].create_operation(session['id'],operation_type='development',product_id=orange.id,quantity_text='0.1',development_title='Новая проработка',actor_user_id=10)
    renamed=environment['corrections'].rename_development(session['id'],development['id'],'Исправленная проработка',expected_revision=development['revision'],actor_user_id=10);assert renamed['development_title']=='Исправленная проработка'


def test_correction_mutation_requires_active_session_and_preserves_old_snapshot(environment):
    base,session=_open(environment);before=[dict(line) for line in base['lines']];orange=_product(environment,'Апельсин')
    existing=environment['operations'].repository.list_operations(base['shift_id'])[0]
    with pytest.raises(WarehouseOperationsError,match='not open'):environment['operations'].update_quantity(existing['id'],'1',None,expected_revision=existing['revision'],actor_user_id=20)
    with pytest.raises(WarehouseCorrectionError):environment['corrections'].update_quantity(session['id']+999,existing['id'],'1',None,expected_revision=existing['revision'],actor_user_id=10)
    environment['corrections'].update_quantity(session['id'],existing['id'],'0.5',None,expected_revision=existing['revision'],actor_user_id=10)
    assert WarehouseReportsDatabase(environment['path']).get_report(base['id'])['lines']==before
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(environment['path']) as connection:connection.execute("UPDATE warehouse_report_lines SET quantity_scaled=1 WHERE report_id=?",(base['id'],))


def test_correction_stale_operation_and_session_revision_do_not_write(environment):
    base,session=_open(environment);operation=environment['operations'].repository.list_operations(base['shift_id'])[0];before=dict(operation)
    with pytest.raises(WarehouseCorrectionConflict):environment['corrections'].update_quantity(session['id'],operation['id'],'0.9',None,expected_revision=operation['revision']-1,actor_user_id=10)
    assert environment['operations'].repository.get_operation(operation['id'])==before
    with pytest.raises(WarehouseCorrectionConflict):environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision']-1,actor_user_id=10)
    with sqlite3.connect(environment['path']) as connection:assert connection.execute("SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",(base['shift_id'],)).fetchone()[0]==1


def test_correction_draft_does_not_create_new_version(environment,tmp_path):
    base,session=_open(environment)
    with pytest.raises(Exception):environment['reports'].draft(actor_user_id=10,directory=tmp_path)
    with sqlite3.connect(environment['path']) as connection:assert connection.execute("SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",(base['shift_id'],)).fetchone()[0]==1


def test_correction_cancel_only_before_first_mutation(environment):
    base,session=_open(environment);cancelled=environment['corrections'].cancel_correction(session['id'],expected_revision=session['revision'],actor_user_id=10)
    assert cancelled['status']=='cancelled' and environment['operations'].repository.shift(base['shift_id'])['status']=='closed'

    shift=environment['operations'].repository.shift(base['shift_id']);again=environment['corrections'].open_correction(base['shift_id'],'Вторая попытка',expected_shift_revision=shift['revision'],actor_user_id=10)
    environment['corrections'].create_operation(again['id'],operation_type='writeoff',product_id=_product(environment,'Апельсин').id,quantity_text='0.1',actor_user_id=10)
    current=environment['corrections'].session(again['id'],actor_user_id=10)
    with pytest.raises(WarehouseCorrectionError,match='mutations'):environment['corrections'].cancel_correction(again['id'],expected_revision=current['revision'],actor_user_id=10)


def test_correction_finalize_v001_to_v002_three_sheets_and_immutable_previous(environment):
    base,session=_open(environment);old_bytes=Path(environment['project']/base['relative_file_path']).read_bytes();old_meta=(base['file_sha256'],base['file_size_bytes'],base['ready_at_utc'])
    environment['corrections'].create_operation(session['id'],operation_type='writeoff',product_id=_product(environment,'Апельсин').id,quantity_text='0.2',actor_user_id=10)
    current=environment['corrections'].session(session['id'],actor_user_id=10);result=environment['corrections'].finalize_correction(session['id'],expected_revision=current['revision'],actor_user_id=10)
    report=result['report'];assert report['version']==2 and report['status']=='ready' and report['supersedes_report_id']==base['id'] and report['correction_session_id']==session['id']
    with zipfile.ZipFile(result['artifact'].path) as archive:
        workbook=archive.read('xl/workbook.xml').decode();assert all(name in workbook for name in ('Списание','Перемещение','Проработка'))
    unchanged=WarehouseReportsDatabase(environment['path']).get_report(base['id']);assert (unchanged['file_sha256'],unchanged['file_size_bytes'],unchanged['ready_at_utc'])==old_meta and Path(environment['project']/unchanged['relative_file_path']).read_bytes()==old_bytes


def test_correction_subsequent_version_and_duplicate_finalize_are_idempotent(environment):
    base,session=_open(environment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,repeat=list(pool.map(lambda _:environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision'],actor_user_id=10),range(2)))
    assert first['report']['id']==repeat['report']['id'] and first['report']['version']==2
    shift=environment['operations'].repository.shift(base['shift_id']);next_session=environment['corrections'].open_correction(base['shift_id'],'Ещё одно исправление',expected_shift_revision=shift['revision'],actor_user_id=10);third=environment['corrections'].finalize_correction(next_session['id'],expected_revision=next_session['revision'],actor_user_id=10)
    assert third['report']['version']==3 and third['report']['supersedes_report_id']==first['report']['id']
    with sqlite3.connect(environment['path']) as connection:assert connection.execute("SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",(base['shift_id'],)).fetchone()[0]==3


def test_correction_required_checklist_recalculated_and_blocks_finalize(environment):
    orange=_product(environment,'Апельсин')
    with sqlite3.connect(environment['path']) as connection:
        connection.execute("INSERT INTO warehouse_required_rules(stable_id,product_id,operation_type,quantity_mode,suggested_quantity_scaled,base_unit,weekdays_mask,question_text,sort_order,allow_not_applicable,is_active,created_at_utc,updated_at_utc,revision) VALUES('required-orange',?,'writeoff','recommended',100000,'kg',127,'Апельсин?',1,1,1,'2026-09-10T10:00:00Z','2026-09-10T10:00:00Z',1)",(orange.id,));connection.commit()
    session=environment['closing'].start(actor_user_id=20)
    for requirement in session['requirements']:
        if requirement['status']!='pending':continue
        if requirement['product_id']==orange.id:session=environment['closing'].recommended(session['id'],requirement['id'],actor_user_id=20)
        else:session=environment['closing'].mark_not_applicable(session['id'],requirement['id'],'Нет остатка',actor_user_id=20)
    session=environment['closing'].resume(session['id'],actor_user_id=20);environment['closing'].finalize(session['id'],expected_revision=session['revision'],actor_user_id=20);base=environment['reports'].build_final(session['shift_id'],actor_user_id=20)['report']
    shift=environment['operations'].repository.shift(base['shift_id']);correction=environment['corrections'].open_correction(base['shift_id'],'Убрать ошибочное',expected_shift_revision=shift['revision'],actor_user_id=10);operation=environment['operations'].repository.list_operations(base['shift_id'])[0];environment['corrections'].delete_operation(correction['id'],operation['id'],expected_revision=operation['revision'],reason='ошибка',actor_user_id=10)
    current=environment['corrections'].session(correction['id'],actor_user_id=10);result=environment['corrections'].finalize_correction(correction['id'],expected_revision=current['revision'],actor_user_id=10)
    assert result['ready'] is False and result['missing'][0]['product_name']=='Апельсин'
    with sqlite3.connect(environment['path']) as connection:assert connection.execute("SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",(base['shift_id'],)).fetchone()[0]==1


def test_correction_build_failure_is_recoverable_and_blocks_mutation(environment,monkeypatch):
    _,session=_open(environment);original=environment['xlsx'].build_final
    monkeypatch.setattr(environment['xlsx'],'build_final',lambda report:(_ for _ in ()).throw(RuntimeError('build failed')))
    with pytest.raises(RuntimeError,match='build failed'):environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision'],actor_user_id=10)
    failed=environment['corrections'].session(session['id'],actor_user_id=10);assert failed['status']=='failed' and failed['shift_status']=='closing'
    with pytest.raises(WarehouseCorrectionError):environment['corrections'].create_operation(session['id'],operation_type='writeoff',product_id=_product(environment,'Апельсин').id,quantity_text='0.1',actor_user_id=10)
    monkeypatch.setattr(environment['xlsx'],'build_final',original);recovered=environment['corrections'].recover_correction(session['id'],actor_user_id=10);assert recovered['report']['status']=='ready'


def test_correction_recovery_with_published_file_and_ready_unfinished_session(environment):
    _,session=_open(environment);prepared=environment['corrections'].repository.begin_finalize(session['id'],expected_revision=session['revision'],actor_user_id=10,now=environment['corrections']._now());artifact=environment['xlsx'].build_final(prepared['report'])
    recovered=environment['corrections'].recover_correction(session['id'],actor_user_id=10);assert recovered['artifact'].file_sha256==artifact.file_sha256

    shift=environment['operations'].repository.shift(prepared['report']['shift_id']);second=environment['corrections'].open_correction(shift['id'],'Ready recovery',expected_shift_revision=shift['revision'],actor_user_id=10);prepared2=environment['corrections'].repository.begin_finalize(second['id'],expected_revision=second['revision'],actor_user_id=10,now=environment['corrections']._now());artifact2=environment['xlsx'].build_final(prepared2['report'])
    with sqlite3.connect(environment['path']) as connection:
        connection.execute("UPDATE warehouse_reports SET status='ready',relative_file_path=?,file_sha256=?,file_size_bytes=?,ready_at_utc=? WHERE id=?",(artifact2.relative_file_path,artifact2.file_sha256,artifact2.file_size_bytes,environment['corrections']._now(),prepared2['report']['id']));connection.commit()
    recovered2=environment['corrections'].recover_correction(second['id'],actor_user_id=10);assert recovered2['session']['status']=='completed'


def test_correction_report_build_runs_outside_sqlite_transaction(environment):
    _,session=_open(environment);real=environment['xlsx']
    class CheckingXlsx:
        def build_final(self,report):
            with sqlite3.connect(environment['path'],timeout=.1,isolation_level=None) as connection:connection.execute('BEGIN IMMEDIATE');connection.rollback()
            return real.build_final(report)
        def verify_ready(self,report):return real.verify_ready(report)
    environment['corrections'].xlsx=CheckingXlsx();result=environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision'],actor_user_id=10);assert result['report']['status']=='ready'


def _recipient(path,chat,title):
    with sqlite3.connect(path) as connection:
        value=connection.execute("INSERT INTO warehouse_report_recipients(chat_id,title,chat_type,is_active,created_by,created_at_utc,updated_at_utc,revision) VALUES(?,?,'group',1,10,'2026-09-10T10:00:00Z','2026-09-10T10:00:00Z',1)",(chat,title)).lastrowid;connection.commit();return value


def test_correction_delivery_uses_successful_base_recipients_and_separate_retry(environment):
    base,session=_open(environment,'Исправить <остаток>');first=_recipient(environment['path'],-100,'Получал v001');_recipient(environment['path'],-101,'Не получал v001')
    with sqlite3.connect(environment['path']) as connection:
        connection.execute("INSERT INTO warehouse_report_deliveries(report_id,recipient_id,status,attempt_count,telegram_message_id,last_attempt_at_utc,delivered_at_utc,created_at_utc,updated_at_utc) VALUES(?,?,'delivered',1,77,'2026-09-10T20:00:00Z','2026-09-10T20:00:00Z','2026-09-10T20:00:00Z','2026-09-10T20:00:00Z')",(base['id'],first));connection.commit()
    result=environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision'],actor_user_id=10);preview=environment['corrections'].delivery_preview(session['id'],actor_user_id=10);assert preview['recipient_ids']==(first,)
    class Sender:
        def __init__(self):self.calls=[];self.fail=True
        async def send_report(self,**kwargs):
            self.calls.append(kwargs)
            if self.fail:raise RuntimeError('telegram')
            return 99
    sender=Sender();delivery=WarehouseReportDeliveryService(WarehouseDeliveryDatabase(environment['path']),environment['reports'],environment['access'],sender,clock=environment['clock'])
    failed=asyncio.run(environment['corrections'].deliver_correction(session['id'],delivery,actor_user_id=10));assert failed['failed']==1 and 'Исправленный отчёт' in sender.calls[0]['caption'] and 'v002' in sender.calls[0]['caption'] and '&lt;остаток&gt;' in sender.calls[0]['caption']
    sender.fail=False;sent=asyncio.run(environment['corrections'].deliver_correction(session['id'],delivery,actor_user_id=10,retry_failed=True));assert sent['delivered']==1
    with sqlite3.connect(environment['path']) as connection:
        assert connection.execute("SELECT status,telegram_message_id FROM warehouse_report_deliveries WHERE report_id=?",(base['id'],)).fetchone()==('delivered',77)
        assert connection.execute("SELECT COUNT(*) FROM warehouse_report_deliveries WHERE report_id=?",(result['report']['id'],)).fetchone()[0]==1


def test_correction_delivery_falls_back_to_all_active_recipients(environment):
    _,session=_open(environment);first=_recipient(environment['path'],-200,'Первый');second=_recipient(environment['path'],-201,'Второй');environment['corrections'].finalize_correction(session['id'],expected_revision=session['revision'],actor_user_id=10)
    preview=environment['corrections'].delivery_preview(session['id'],actor_user_id=10);assert set(preview['recipient_ids'])=={first,second}
