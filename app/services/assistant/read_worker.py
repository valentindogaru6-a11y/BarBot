"""One exec, one bounded request, fixed risk-0 composition, then exit."""
from __future__ import annotations

from time import monotonic
_START = monotonic()
from pathlib import Path
import resource
import socket
import sqlite3
import sys
from zoneinfo import ZoneInfo
from pydantic import BaseModel, TypeAdapter

# All imports complete before the OS filter. No Settings/.env, DB initializer,
# Telegram transport, report generation or provider client runs at import time.
from app.config import Settings
from app.models.assistant_capabilities import CapabilityRisk, CapabilityResult
from app.models.assistant_planner import PlannerContextSnapshot, PlannerDecision, PlannerTargetOption
from app.models.assistant_tools import GetTechCardInput
from app.models.catalog import Catalog
from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.services.assistant.capabilities import CAPABILITIES, CapabilityBinding, CapabilityRegistry
from app.services.assistant.capability_controller import AssistantCapabilityController
from app.services.assistant.orchestrator import AssistantToolRegistry
from app.services.assistant.tools import AssistantReadOnlyTools
from app.services.assistant import read_only as ro
from app.services.assistant.read_broker import IN_WORKER, actor_reference, encode_action, encode_dispatch
from app.services.assistant.read_protocol import ReadRequest, ReadResponse, MAX_REQUEST, MAX_RESPONSE, encode
from app.services.assistant.read_kernel import install, seal
from app.services.assistant.planner_policy import mark_tool_boundary
from app.services.assistant.read_results import ReadData
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.xlsx_report_service import WarehouseXlsxReportService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.schedule_service import WarehouseScheduleService
from app.storage.user_database import UserDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase
from app.storage.assistant_read_snapshot import business_fingerprint, integrity_ok


class SnapshotLease:
    """Private worker lease: repositories cannot end the enclosing read snapshot.

    No lease/connection/cursor can cross JSON. Native connection lifetime belongs
    exclusively to _execute's finally, including scope/serialization exceptions.
    """
    def __init__(self, connection):
        self._connection = connection

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        if value is not sqlite3.Row:
            raise ro.ReadOnlyViolation('read.worker', 'row_factory')

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, statement, parameters=()):
        if statement.strip().upper() == 'BEGIN':
            return self._connection.execute('SELECT 1')
        return self._connection.execute(statement, parameters)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _compose(request, connection):
    path = Path(request.database)
    facts = request.access
    actors = [row[0] for row in connection.execute('SELECT telegram_user_id FROM users')
              if actor_reference(request.request_id, row[0]) == facts.actor_ref]
    if len(actors) != 1:
        raise ro.ReadOnlyViolation(request.capability_id, 'access')
    actor = actors[0]
    users = UserService(UserDatabase(path))
    access = AuthService(frozenset({actor}) if facts.role is Role.SUPER_ADMIN else frozenset(), users)
    context = access.get_context(actor)
    if (not facts.private or not facts.eligible or not facts.active or not facts.ai_enabled
            or context.user is None or not context.user.is_active or context.role != facts.role
            or context.role in {Role.USER, Role.BLOCKED}
            or facts.mode == 'pilot_allowlist' and (facts.role is not Role.SUPER_ADMIN or not facts.pilot_allowed)):
        raise ro.ReadOnlyViolation(request.capability_id, 'access')
    settings = Settings(_env_file=None, bot_token='isolated-read',
        ai_assistant_enabled=facts.ai_enabled, warehouse_enabled=facts.warehouse_enabled,
        ai_access_mode=facts.mode, ai_pilot_user_ids=str(actor) if facts.pilot_allowed else '')
    registry = CapabilityRegistry(); policy = AssistantAccessPolicy(access, registry, settings)
    operations = WarehouseOperationsService(WarehouseOperationsDatabase(path), access)
    attendance = WarehouseAttendanceService(WarehouseAttendanceDatabase(path), access)
    closing = WarehouseClosingService(WarehouseClosingDatabase(path), operations, access)
    reports = WarehouseReportService(WarehouseReportsDatabase(path), operations, access, path.parent,
        xlsx_service=WarehouseXlsxReportService(path.parent, project_root=path.parent))
    recipients = WarehouseRecipientService(WarehouseDeliveryDatabase(path), access)
    reminders = WarehouseReminderSettingsService(WarehouseReminderDatabase(path), access)
    schedules = WarehouseScheduleService(WarehouseScheduleDatabase(path), None, access)
    flags = dict(ai_assistant_enabled=facts.ai_enabled, warehouse_enabled=facts.warehouse_enabled)
    ctrl = AssistantCapabilityController(registry, None, access, policy, flags=flags,
        users=users, attendance=attendance, operations=operations, closing=closing,
        reports=reports, recipients=recipients, reminders=reminders, schedules=schedules)
    def forbidden(*args, **kwargs):
        raise ro.ReadOnlyViolation(request.capability_id, 'contract')
    # Parent binding metadata is used for dynamic help, never as executable IPC.
    registry.bind({c.id: CapabilityBinding(c.id, ctrl, ctrl, 'isolated', 'typed', forbidden)
                   for c in CAPABILITIES if c.id in facts.bound})
    available = {c.id for c in policy.available_capabilities(actor)}
    if request.capability_id not in available.intersection(facts.available):
        raise ro.ReadOnlyViolation(request.capability_id, 'access')
    tools = AssistantReadOnlyTools(request.catalog or Catalog(version=1), access,
        users=users, attendance=attendance, operations=operations, closing=closing,
        reports=reports, recipients=recipients, reminders=reminders, schedules=schedules,
        capabilities=registry, flags=flags, access_policy=policy)
    return actor, ctrl, tools


def _run(request, connection):
    if request.mode == 'catalog':
        if request.access is not None:
            _compose(request, connection)
        return encode_action(AssistantToolRegistry(request.catalog).execute(request.catalog_intent))
    actor, controller, tools = _compose(request, connection)
    definition = controller.registry.require(request.capability_id)
    if definition.risk_level is not CapabilityRisk.READ:
        raise ro.ReadOnlyViolation(request.capability_id, 'contract')
    if request.mode == 'tool':
        function = getattr(tools, definition.adapter, None)
        if not callable(function):
            raise ro.ReadOnlyViolation(request.capability_id, 'contract')
        payload = (GetTechCardInput(card_id=request.arguments['entity_ref'])
                   if definition.id == 'catalog.get' else definition.input_model.model_validate(request.arguments))
        result = function(actor, payload)
        if isinstance(result, CapabilityResult):
            ReadData.model_validate(result.data)
        from typing import get_type_hints
        return TypeAdapter(get_type_hints(function)['return']).dump_python(result, mode='json')
    domain = {'navigation':'general', 'help':'general', 'recipients':'administration',
        'reminders':'administration', 'schedules':'administration'}.get(definition.category, definition.category)
    args = dict(request.arguments); target = args.get('entity_ref')
    options = tuple(PlannerTargetOption(stable_id=key, display_name='candidate', entity_kind='tech_card',
        domain='catalog', relation_label='candidate') for key in request.candidate_ids)
    if target is not None and target not in request.candidate_ids and options:
        options += (PlannerTargetOption(stable_id=target,display_name='selected',entity_kind='tech_card',
            domain=domain,relation_label='last_successful'),)
    snapshot = PlannerContextSnapshot(actor_role=request.access.role.value,
        available_capability_ids=request.access.available, candidate_entity_ids=(target,) if target else (),
        target_options=options)
    decision = PlannerDecision(dialogue_act='new_request', domain=domain, capability_id=definition.id,
        target_entity_id=target, arguments=args, confidence=10000, needs_clarification=False,
        clarification_question=None, read_only=True, mutation=False, context_updates={})
    return encode_dispatch(controller.dispatch_plan(decision, snapshot=snapshot,
        catalog=request.catalog or Catalog(version=1), actor_user_id=actor,
        source_chat_id=actor, source_message_id=0, chat_type='private'))


def _execute(request, facts, control):
    connection = None; original = sqlite3.connect; denied = []
    token = IN_WORKER.set(True)
    try:
        if request.database:
            connection = original(Path(request.database).as_uri()+'?mode=ro', uri=True,
                isolation_level=None, timeout=.25)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            def authorizer(action, name, value, db, source):
                # Verification SQL has the same deny-write policy even outside
                # the nested tool scope; no gap before/after executor entry.
                token = ro._ACTIVE.set(ro._Scope(request.capability_id)) if ro._ACTIVE.get() is None else None
                try:
                    result = ro.authorize_sql(action, name, value, db, source)
                finally:
                    if token is not None:
                        ro._ACTIVE.reset(token)
                if result != sqlite3.SQLITE_OK:
                    denied.append(action)
                return result
            connection.set_authorizer(authorizer)
            connection.execute('BEGIN')
            facts['business_before'] = business_fingerprint(connection)
            facts['integrity'] = integrity_ok(connection)
            def connect(path, *args, **kwargs):
                if str(path) != request.database or kwargs.get('factory') or kwargs.get('uri'):
                    raise ro.ReadOnlyViolation(request.capability_id, 'path')
                return SnapshotLease(connection)
            sqlite3.connect = connect
        seal(control)
        with ro.read_only_execution(request.capability_id):
            result = _run(request, connection)
        if connection:
            connection.rollback()
            connection.execute('BEGIN')
            facts['business_after'] = business_fingerprint(connection)
            facts['integrity'] = facts['integrity'] and integrity_ok(connection)
            facts['business_writes'] = connection.total_changes
            facts['authorizer_denials'] = len(denied)
            if facts['business_before'] != facts['business_after'] or denied or not facts['integrity']:
                raise ro.ReadOnlyViolation(request.capability_id, 'state_changed')
        return result
    finally:
        try:
            if connection:
                facts['business_writes'] = connection.total_changes
                facts['authorizer_denials'] = len(denied)
            if connection:
                try:
                    connection.rollback()
                finally:
                    connection.close()
        finally:
            sqlite3.connect = original
            IN_WORKER.reset(token)


def main(control_fd):
    raw = sys.stdin.buffer.read(MAX_REQUEST+1)
    try:
        if len(raw) > MAX_REQUEST:
            raise ValueError
        request = ReadRequest.model_validate_json(raw)
    except BaseException:
        raise SystemExit(2) from None
    control = socket.socket(fileno=control_fd)
    # Warm local implementation dependencies; never a provider/Telegram call.
    ZoneInfo('Europe/Kyiv'); ZoneInfo('UTC')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
    started = monotonic(); facts = {}
    status, category, result = 'ok', 'ok', None
    try:
        install(control)
        if monotonic() >= request.deadline:
            raise TimeoutError
        result = _execute(request, facts, control)
    except ro.ReadOnlyViolation:
        status, category = 'safety', 'read_only'
    except BaseExceptionGroup as error:
        status, category = ('safety', 'read_only') if ro.read_only_failure(error) else ('infrastructure', 'executor')
    except sqlite3.Error as error:
        infrastructure = getattr(error,'sqlite_errorcode',0) & 255 in {
            sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_CANTOPEN,sqlite3.SQLITE_IOERR}
        status, category = ('infrastructure','executor') if infrastructure else ('safety','contract')
    except TimeoutError:
        status, category = 'infrastructure', 'deadline'
    except BaseException:
        status, category = 'infrastructure', 'executor'
    if status != 'ok':
        result = None
    if control.fileno() >= 0:
        control.close()
    try:
        response = ReadResponse(request_id=request.request_id, manifest_hash=request.manifest_hash,
            capability_id=request.capability_id, status=status, category=category, result=result,
            startup_ms=(started-_START)*1000, execution_ms=(monotonic()-started)*1000,
            rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, **facts)
        output = encode(response)
        if len(output) > MAX_RESPONSE:
            raise ValueError
    except BaseException:
        output = encode(ReadResponse(request_id=request.request_id, manifest_hash=request.manifest_hash,
            capability_id=request.capability_id, status='safety', category='serialization',
            startup_ms=0, execution_ms=0, rss_kib=0))
    sys.stdout.buffer.write(output); sys.stdout.buffer.flush()
