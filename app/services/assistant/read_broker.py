"""Bounded exec/JSON broker with kernel-mediated SQLite file authority."""
from __future__ import annotations

import atexit
import asyncio
from contextvars import ContextVar, copy_context
from functools import partial
from dataclasses import asdict, fields
import hashlib
import errno
import json
import re
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import threading
from time import monotonic
from uuid import uuid4

from pydantic import TypeAdapter

from app.services.assistant.read_only import ReadOnlyViolation
from app.services.assistant.read_protocol import (
    AccessFacts, ReadRequest, ReadResponse, MAX_REQUEST, MAX_RESPONSE, encode, manifest,
)
from app.services.assistant.read_kernel import FileAuthority, NotificationWakeup, file_identity, receive_listener, receive_seal
from app.services.assistant.read_results import ReadDispatch

IN_WORKER = ContextVar('assistant_read_worker', default=False)
_CANCEL = ContextVar('assistant_read_cancel', default=None)
_CHILDREN = set()
_LOCK = threading.Lock()
_PERMITS = threading.BoundedSemaphore(4)
PROJECT = Path(__file__).resolve().parents[3]


class ReadWorkerFailure(BaseException):
    """Infrastructure control failure: never enter generic/provider fallback."""
    def __init__(self, category):
        self.category = category
        super().__init__('isolated_read_infrastructure')


def _stop(child):
    if child.poll() is None:
        child.kill()
    child.wait(timeout=3)
    for pipe in (child.stdin, child.stdout):
        if pipe:
            pipe.close()


def shutdown_workers():
    with _LOCK:
        children = tuple(_CHILDREN)
    for child in children:
        _stop(child)


atexit.register(shutdown_workers)


async def await_read(function, *args, **kwargs):
    """Async owner waits for kill/reap even when it is cancelled repeatedly."""
    cancel = threading.Event()
    token = _CANCEL.set(cancel)
    try:
        # An executor Future is deliberately not an asyncio Task: global task
        # cancellation on shutdown must not cancel the owner before kill/reap.
        task = asyncio.get_running_loop().run_in_executor(None, copy_context().run,
            partial(function,*args,**kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
    finally:
        _CANCEL.reset(token)


def actor_reference(request_id, actor):
    return hashlib.sha256(f'{request_id}:{actor}'.encode()).hexdigest()


def access_facts(access, actor, policy, flags, registry, request_id, chat_type):
    from app.services.assistant.capabilities import CAPABILITIES
    declared={c.id:c for c in CAPABILITIES}
    if any(declared.get(c.id)!=c for c in registry.all):
        raise ReadOnlyViolation('unknown','manifest')
    context = access.get_context(actor)
    settings = policy.settings if policy else None
    eligible = policy.can_enter_assistant(actor, chat_type=chat_type) if policy else bool(
        context.user and context.user.is_active and context.role.value not in {'user', 'blocked'}
        and chat_type == 'private' and flags.get('ai_assistant_enabled', True))
    available = (policy.available_capabilities(actor, chat_type=chat_type) if policy else
        registry.available_for_actor(access, actor, flags=flags, chat_type=chat_type))
    return AccessFacts(actor_ref=actor_reference(request_id, actor), role=context.role,
        active=bool(context.user and context.user.is_active), private=chat_type == 'private', eligible=eligible,
        mode=settings.ai_access_mode if settings else 'role_based',
        pilot_allowed=bool(settings and actor in settings.ai_pilot_user_ids),
        ai_enabled=flags.get('ai_assistant_enabled', True), warehouse_enabled=flags.get('warehouse_enabled', True),
        available=tuple(c.id for c in available), bound=tuple(registry.bindings))


def _main_fingerprint(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
        after = os.fstat(fd)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
                            s.st_size, s.st_atime_ns, s.st_mtime_ns, s.st_ctime_ns)
        if fields(before) != fields(after) or before.st_nlink != 1:
            raise ValueError('read_main_changed')
        return fields(after), digest.hexdigest()
    finally:
        os.close(fd)


def _entries(root, approved):
    """Sibling metadata only; no reports/audit contents are ever read."""
    return {p.name: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        for p in root.iterdir() if p.name not in approved for s in (p.lstat(),)}


class ReadOnlyExecutionBroker:
    def __init__(self, *, timeout=12.):
        if not 0 < timeout <= 30:
            raise ValueError('read_deadline')
        self.timeout = timeout
        self.last_metrics = None
        self._approved = None

    def request(self, identifier, *, access=None, actor=None, policy=None, flags=None,
                registry=None, catalog=None, arguments=None, mode='dispatch', intent=None,
                candidates=(), chat_type='private'):
        rid = uuid4().hex
        path = Path(access.user_service.database.path).absolute() if access and access.user_service else None
        request = ReadRequest(request_id=rid, manifest_hash=manifest(), capability_id=identifier, mode=mode,
            database=str(path) if path else None, database_identity=file_identity(path) if path else None,
            access=access_facts(access, actor, policy, flags or {}, registry, rid, chat_type) if access else None,
            catalog=catalog, arguments=arguments or {}, catalog_intent=intent, candidate_ids=candidates,
            deadline=monotonic()+self.timeout)
        self._approved = hashlib.sha256(encode(request)).digest()
        return request

    def execute(self, request):
        try:
            raw = encode(request)
            if self._approved is None or hashlib.sha256(raw).digest() != self._approved:
                raise ValueError
            self._approved = None
            if len(raw) > MAX_REQUEST:
                raise ValueError
            request = ReadRequest.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ReadOnlyViolation('unknown', 'read_ipc_contract') from None
        from app.services.assistant.planner_policy import mark_tool_boundary
        mark_tool_boundary()
        if not _PERMITS.acquire(blocking=False):
            raise ReadWorkerFailure('capacity')
        child = authority = listener = control = child_control = None
        before_main = before_entries = None
        sealed = False
        try:
            if request.database:
                try:
                    authority = FileAuthority(request.database)
                except ValueError:
                    raise ReadOnlyViolation(request.capability_id, 'path') from None
                except RuntimeError:
                    raise ReadWorkerFailure('sandbox') from None
                if authority.main != request.database_identity:
                    raise ReadOnlyViolation(request.capability_id, 'path')
                before_main = _main_fingerprint(request.database)
                before_entries = _entries(authority.path.parent, set(authority.identities))
            control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            bootstrap = (f'import sys;sys.path.insert(0,{str(PROJECT)!r});'
                         f'from app.services.assistant.read_worker import main;main({child_control.fileno()})')
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap], cwd=PROJECT,
                env={'LANG':'C.UTF-8', 'TZ':'UTC'}, close_fds=True, pass_fds=(child_control.fileno(),),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            child_control.close(); child_control = None
            with _LOCK:
                _CHILDREN.add(child)
            os.set_blocking(child.stdin.fileno(), False); os.set_blocking(child.stdout.fileno(), False)
            output = bytearray(); offset = 0; eof = False
            cancel = _CANCEL.get()
            with NotificationWakeup(), selectors.DefaultSelector() as selector:
                selector.register(control, selectors.EVENT_READ, 'control')
                selector.register(child.stdin, selectors.EVENT_WRITE, 'input')
                selector.register(child.stdout, selectors.EVENT_READ, 'output')
                while not eof:
                    if (cancel is not None and cancel.is_set()) or monotonic() >= request.deadline:
                        raise ReadWorkerFailure('cancelled' if cancel and cancel.is_set() else 'timeout')
                    for key, _ in selector.select(.025):
                        if key.data == 'control':
                            if listener is None:
                                try:
                                    listener = receive_listener(control)
                                except RuntimeError:
                                    raise ReadWorkerFailure('sandbox_handshake') from None
                                selector.register(listener, selectors.EVENT_READ, 'kernel')
                            else:
                                try:
                                    packet = receive_seal(control)
                                except RuntimeError:
                                    raise ReadOnlyViolation(request.capability_id,'sandbox_protocol') from None
                                if packet == b'S' and not sealed:
                                    if authority:
                                        authority.check(); authority.sealed = True
                                    sealed = True
                                    control.send(b'A')
                                elif packet:
                                    raise ReadOnlyViolation(request.capability_id, 'sandbox_protocol')
                                selector.unregister(control); control.close(); control = None
                        elif key.data == 'kernel':
                            if child.poll() is not None:
                                selector.unregister(listener)
                                continue
                            if authority is None:
                                # Pure catalog calls have no filesystem authority.
                                from app.services.assistant.read_kernel import Notification, Response, _ioctl
                                notification = Notification()
                                try:
                                    _ioctl(listener, 0xc0502100, notification)
                                except OSError as error:
                                    if error.errno in (errno.EINTR, errno.ENOENT):
                                        continue
                                    raise
                                _ioctl(listener, 0xc0182101, Response(notification.id, 0, -1, 0))
                                raise ReadOnlyViolation(request.capability_id, 'kernel_authority')
                            try:
                                authority.handle(listener)
                            except OSError as error:
                                if error.errno in (errno.EINTR, errno.ENOENT):
                                    continue
                                raise
                        elif key.data == 'input':
                            try:
                                offset += os.write(child.stdin.fileno(), raw[offset:offset+8192])
                            except BrokenPipeError:
                                offset = len(raw)
                            if offset == len(raw):
                                selector.unregister(child.stdin); child.stdin.close()
                        else:
                            chunk = os.read(child.stdout.fileno(), 8192)
                            if not chunk:
                                eof = True; break
                            output.extend(chunk)
                            if len(output) > MAX_RESPONSE:
                                raise ReadWorkerFailure('oversized_ipc')
            child.wait(timeout=max(.01, min(1., request.deadline-monotonic())))
            if child.returncode:
                raise ReadWorkerFailure('crash')
            if listener is None:
                raise ReadWorkerFailure('missing_sandbox')
            try:
                response = ReadResponse.model_validate_json(output)
            except ValueError:
                raise ReadWorkerFailure('malformed_ipc') from None
            if (response.request_id != request.request_id or response.manifest_hash != request.manifest_hash
                    or response.capability_id != request.capability_id):
                raise ReadOnlyViolation(request.capability_id, 'response_contract')
            self.last_metrics = dict(startup_ms=response.startup_ms, execution_ms=response.execution_ms,
                rss_kib=response.rss_kib, business_writes=response.business_writes,
                authorizer_denials=response.authorizer_denials)
            if response.status == 'safety' or response.business_writes or response.authorizer_denials or not response.integrity:
                raise ReadOnlyViolation(request.capability_id, response.category)
            if response.status != 'ok':
                raise ReadWorkerFailure(response.category)
            if not sealed:
                raise ReadOnlyViolation(request.capability_id, 'unsealed_execution')
            if ((request.database and not response.business_before)
                    or response.business_before != response.business_after):
                raise ReadOnlyViolation(request.capability_id, 'business_changed')
            return response.result
        except (OSError, subprocess.SubprocessError):
            raise ReadWorkerFailure('process') from None
        finally:
            verification_error = None
            try:
                if child:
                    _stop(child)
                if authority:
                    try:
                        authority.check()
                        if (authority.violation or before_main != _main_fingerprint(request.database)
                                or before_entries != _entries(authority.path.parent, set(authority.identities))):
                            raise ValueError('read_filesystem_changed')
                    except (ValueError, OSError):
                        verification_error = ReadOnlyViolation(request.capability_id, 'filesystem_boundary')
            finally:
                if listener is not None:
                    os.close(listener)
                if control:
                    control.close()
                if child_control:
                    child_control.close()
                if authority:
                    authority.close()
                with _LOCK:
                    _CHILDREN.discard(child)
                _PERMITS.release()
            if verification_error:
                raise verification_error

    def dispatch(self, controller, identifier, actor, *, catalog=None, arguments=None, candidates=(), chat_type='private'):
        value = self.execute(self.request(identifier, access=controller.access, actor=actor,
            policy=controller.access_policy, flags=controller.flags, registry=controller.registry,
            catalog=catalog, arguments=arguments, candidates=candidates, chat_type=chat_type))
        result = decode_dispatch(value)
        if result.capability_id != identifier:
            raise ReadOnlyViolation(identifier, 'result_capability')
        if result.tool_action is not None:
            validate_catalog_targets(catalog,result.tool_action)
        return result

    def catalog(self, catalog, intent, *, context=None):
        result=decode_action(self.execute(self.request('catalog.get' if intent.selected_entity_id else 'catalog.search',
            catalog=catalog, mode='catalog', intent=intent, **(context or {}))))
        validate_catalog_targets(catalog,result)
        return result


def catalog_keys(catalog):
    if catalog is None:
        return set()
    return {f'{category.id}/{subcategory}/{item.id}' for category in catalog.categories if category.is_active
        for subcategory,items in [('_',category.items),*((s.id,s.items) for s in category.subcategories if s.is_active)]
        for item in items if item.is_active}


def validate_catalog_targets(catalog,action):
    allowed=catalog_keys(catalog)
    returned={f'{r.category.id}/{r.subcategory.id if r.subcategory else "_"}/{r.item.id}' for r in action.search_results}
    returned.update(action.candidate_ids);returned.update(action.related_entity_ids)
    returned.update(x for x in (action.parent_entity_id,action.selected_entity_id) if x)
    if not returned<=allowed:
        raise ReadOnlyViolation('catalog.search','result_target')


def encode_action(action):
    value = asdict(action)
    value['search_results'] = [dict(category=dict(id=r.category.id, name=r.category.name),
        subcategory=dict(id=r.subcategory.id, name=r.subcategory.name) if r.subcategory else None,
        item=r.item.model_dump(mode='json')) for r in action.search_results]
    for row in value['search_results']:
        media=row['item'].get('photo_file_id')
        # This is an opaque media reference for the existing renderer, never a
        # user/chat identifier, local pathname or URL fetched by the worker.
        if media is not None and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{19,1023}',media):
            row['item']['photo_file_id']=None
    return json.loads(encode(value))


def decode_action(value):
    from app.services.assistant.orchestrator import AssistantToolAction
    if not isinstance(value, dict) or value.get('kind') not in {'find_tech_card', 'not_found', 'clarify'} or value.get('raw_intake'):
        raise ReadWorkerFailure('result_contract')
    if set(value) != {f.name for f in fields(AssistantToolAction)}:
        raise ReadWorkerFailure('result_contract')
    matches=value.get('search_results')
    if not isinstance(matches,list) or len(matches)>10:
        raise ReadWorkerFailure('result_contract')
    for row in matches:
        if (not isinstance(row,dict) or set(row) != {'category','subcategory','item'}
                or not isinstance(row['category'],dict) or set(row['category']) != {'id','name'}
                or not isinstance(row['item'],dict)
                or (row['subcategory'] is not None and (not isinstance(row['subcategory'],dict) or set(row['subcategory']) != {'id','name'}))):
            raise ReadWorkerFailure('result_contract')
        media=row['item'].get('photo_file_id')
        if media is not None and (not isinstance(media,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{19,1023}',media)):
            raise ReadOnlyViolation('catalog.get','result_media')
    try:
        return TypeAdapter(AssistantToolAction).validate_python(value)
    except (TypeError, ValueError):
        raise ReadWorkerFailure('result_contract') from None


def encode_dispatch(result):
    value = dict(capability_id=result.capability_id, message=result.message, data=result.data,
        ui_action=result.ui_action, tool_action=encode_action(result.tool_action) if result.tool_action else None)
    return ReadDispatch.model_validate(value).model_dump(mode='json', exclude_unset=True)


def decode_dispatch(value):
    from app.services.assistant.capability_controller import CapabilityDispatch
    if not isinstance(value, dict) or set(value) != {'capability_id', 'message', 'data', 'ui_action', 'tool_action'}:
        raise ReadWorkerFailure('result_contract')
    if value['ui_action'] not in (None, 'main_menu'):
        raise ReadWorkerFailure('result_contract')
    try:
        ReadDispatch.model_validate(value)
    except (ValueError,TypeError):
        raise ReadWorkerFailure('result_contract') from None
    return CapabilityDispatch(value['capability_id'], value['message'], data=value['data'], ui_action=value['ui_action'],
        tool_action=decode_action(value['tool_action']) if value['tool_action'] else None)
