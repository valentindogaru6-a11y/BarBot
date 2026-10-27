import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.services.assistant.evaluation_budget import EvaluationSafetyError
from app.services.assistant.evaluation_corpus import LiveCase, LiveInput, LiveExpectation
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.evaluation_observer import FailureCategory
from app.services.assistant.live_evaluation import run_live, budget_coverage
from app.services.assistant.providers import HttpResponse
from scripts import evaluate_assistant_conversations as cli
from tests.test_assistant_live_evaluation import Wire, route, options, settings


READ_CASE = (LiveCase(LiveInput('Read', 'A', 'найди пф для облепихи'),
    LiveExpectation(domain='catalog', capabilities=('catalog.search',), target='sea')),)


@pytest.mark.parametrize('second,field', [
    (route('shell.execute', 'general'), FailureCategory.UNKNOWN),
    (route('catalog.get', 'catalog', 4, action='open'), FailureCategory.TARGET),
    ({**route(), 'permissions': 'unauthorized'}, FailureCategory.WIRE),
])
def test_second_contract_failure_preserves_read_but_cannot_pass(second, field):
    wire = Wire(route('catalog.search', 'catalog', action='search'), second)
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, corpus=READ_CASE))
    assert code == 2 and not report['ready']
    assert report['rows'][0]['target'] == 'sea' and report['rows'][0]['tools'] == 1
    assert report['rows'][0]['error'] is None
    assert report['failure_counts'][field] == 1
    assert report['failure_counts'][FailureCategory.CONTRACT] == 1
    assert report['rows'][0]['failure_counts'][field] == 1
    assert report['rows'][0]['operations'] == 0
    assert report['unauthorized_execution_count'] == 0
    if field == FailureCategory.UNKNOWN:
        assert report['unauthorized_attempt_count'] == 1


@pytest.mark.parametrize('failure,code,field', [
    ('malformed', 2, FailureCategory.WIRE),
    ('timeout', 3, FailureCategory.TRANSPORT),
])
def test_second_step_malformed_or_transport_failure_is_not_hidden(failure, code, field):
    class FailingSecond(Wire):
        async def post_json(self, *args, **kwargs):
            if self.requests:
                self.requests.append(kwargs['payload'])
                if failure == 'timeout':
                    raise TimeoutError()
                return HttpResponse(200, b'{"choices":[{"message":{"content":"{"}}]}')
            return await super().post_json(*args, **kwargs)
    wire = FailingSecond(route('catalog.search', 'catalog', action='search'))
    report, actual_code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, corpus=READ_CASE))
    assert actual_code == code and not report['ready']
    assert report['rows'][0]['target'] == 'sea' and report['rows'][0]['tools'] == 1
    assert report['failure_counts'][field] == 1


def test_valid_second_step_and_authorized_read_pass():
    wire = Wire(route('catalog.search', 'catalog', action='search'), route())
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, corpus=READ_CASE))
    assert code == 0 and report['ready']
    assert not any(report['failure_counts'].values())
    assert report['unauthorized_attempt_count'] == report['unauthorized_execution_count'] == 0
    assert sum(event['executor_invoked'] for event in report['authorization']) == 1


def test_semantic_mismatch_is_not_a_contract_failure():
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=Wire, corpus=READ_CASE))
    assert code == 1 and report['failure_counts'][FailureCategory.SEMANTIC] > 0
    assert report['failure_counts'][FailureCategory.CONTRACT] == 0


def test_unknown_first_capability_records_attempt_without_any_executor():
    wire = Wire(route('shell.execute', 'general'))
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, corpus=READ_CASE))
    assert code == 2 and report['unauthorized_attempt_count'] == 1
    assert report['unauthorized_execution_count'] == 0
    assert not any(event['executor_invoked'] for event in report['authorization'])
    assert report['failure_counts'][FailureCategory.UNKNOWN] == 1


@pytest.mark.parametrize('broken', [False, True])
def test_real_runner_observes_access_denial_and_defective_dispatcher(broken):
    invoked = []
    class BrokenDriver:
        def dispatch(self, event, authorize, execute, observer):
            try:
                authorize(event)
            except EvaluationSafetyError:
                pass
            return observer.invoke(event, execute)
    class DeniedEnvironment(IsolatedEvaluationEnvironment):
        def __enter__(self):
            super().__enter__()
            def denied(*args):
                raise PermissionError('synthetic denial')
            self.controller._require = denied
            if broken:
                self.dispatch_driver = BrokenDriver()
            self.controller.dispatch_plan = lambda *args, **kwargs: invoked.append('executor')
            return self
    wire = Wire(route('catalog.search', 'catalog', action='search'))
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, environment_factory=DeniedEnvironment, corpus=READ_CASE))
    assert code == 2 and not report['ready']
    assert report['unauthorized_attempt_count'] == 1
    assert report['unauthorized_execution_count'] == int(broken)
    assert len(invoked) == int(broken)
    assert report['authorization'][0]['access_authorized'] is False


def test_authorized_mutation_preview_has_no_unauthorized_execution():
    case = LiveCase(LiveInput('Writeoff', 'C', 'спиши апельсин 200 грамм'),
        LiveExpectation(capabilities=('warehouse.prepare_writeoff',), preview=True, argument_ok=True))
    wire = Wire(route('warehouse.prepare_writeoff', 'warehouse', action='create'))
    report, code = asyncio.run(run_live(options(), settings_loader=settings,
        transport_factory=lambda: wire, corpus=(case,)))
    assert code == 0 and report['rows'][0]['preview']
    assert report['rows'][0]['operations'] == 0
    assert report['unauthorized_attempt_count'] == report['unauthorized_execution_count'] == 0


def test_owned_permissions_identity_no_path_injection_and_live_services():
    native = sqlite3.connect
    with IsolatedEvaluationEnvironment() as env:
        assert env.root.stat().st_mode & 0o777 == 0o700
        assert env.path.stat().st_mode & 0o777 == 0o600
        assert env.path.stat().st_nlink == 1
        assert env.state()['schema'] == 15
        with pytest.raises(AttributeError):
            env.path = Path('/tmp/externally-supplied.db')
        assert sqlite3.connect is not native
        with sqlite3.connect(env.path) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('SELECT COUNT(*) FROM users').fetchone()
            for suffix in ('-wal', '-shm'):
                auxiliary = Path(str(env.path)+suffix)
                assert auxiliary.exists() and auxiliary.resolve().parent == env.root
    assert sqlite3.connect is native and not env.root.exists()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')


@pytest.mark.parametrize('kind', ['hardlink', 'symlink', 'directory_symlink', 'traversal', 'relative', 'uri'])
def test_external_paths_and_link_forms_fail_closed(tmp_path, kind):
    outside = tmp_path/'outside.db'
    outside.write_bytes(b'synthetic external file')
    with IsolatedEvaluationEnvironment() as env:
        candidate = env.root/'alias.db'
        if kind == 'hardlink':
            os.link(outside, candidate)
        elif kind == 'symlink':
            candidate.symlink_to(outside)
        elif kind == 'directory_symlink':
            parent = env.root/'linked'
            parent.symlink_to(tmp_path, target_is_directory=True)
            candidate = parent/'outside.db'
        elif kind == 'traversal':
            candidate = env.root/'..'/'outside.db'
        elif kind == 'relative':
            candidate = Path('outside.db')
        else:
            candidate = env.path.as_uri()+'?mode=rw'
        with pytest.raises(EvaluationSafetyError):
            env.validate_path(candidate)
        with pytest.raises(EvaluationSafetyError):
            sqlite3.connect(candidate)
    assert outside.read_bytes() == b'synthetic external file'


def test_main_file_hardlink_is_rejected_before_open_and_restart(tmp_path):
    with IsolatedEvaluationEnvironment() as env:
        os.link(env.path, tmp_path/'external-alias.db')
        for operation in (lambda:sqlite3.connect(env.path), env.recreate):
            with pytest.raises(EvaluationSafetyError):
                operation()


@pytest.mark.parametrize('during_open', [False, True])
def test_inode_replacement_is_detected_before_restart_or_after_native_open(during_open):
    with IsolatedEvaluationEnvironment() as env:
        replacement = env.root/'replacement.db'
        replacement.touch(mode=0o600)
        if during_open:
            native = env._owned.sqlite_open
            def replace_and_connect(*args, **kwargs):
                os.replace(replacement, env.path)
                return native(*args, **kwargs)
            env._owned.sqlite_open = replace_and_connect
            operation = lambda:sqlite3.connect(env.path)
        else:
            os.replace(replacement, env.path)
            operation = env.recreate
        with pytest.raises(EvaluationSafetyError):
            operation()


def test_exclusive_creation_refuses_preexisting_file(monkeypatch):
    from app.services.assistant import evaluation_database as module
    native = module.os.open
    def prepopulate(path, flags, *args, **kwargs):
        if Path(path).name == 'evaluation.db' and flags & os.O_EXCL:
            Path(path).touch(mode=0o600)
        return native(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', prepopulate)
    with pytest.raises(FileExistsError):
        IsolatedEvaluationEnvironment().__enter__()


def test_cleanup_never_removes_replacement_root(tmp_path):
    from app.services.assistant.evaluation_database import OwnedEvaluationDatabase
    owner = OwnedEvaluationDatabase()
    original = owner.root
    original.rename(tmp_path/'moved-owned-root')
    original.mkdir(mode=0o700)
    sentinel = original/'synthetic-sentinel'
    sentinel.touch()
    try:
        with pytest.raises(EvaluationSafetyError):
            owner.close()
        assert sentinel.exists()
    finally:
        sentinel.unlink()
        original.rmdir()


def test_low_cli_budget_fails_before_keys_and_partial_coverage_warns(monkeypatch, capsys):
    calls = []
    def forbidden():
        calls.append('settings')
        raise ValueError('synthetic settings unavailable')
    monkeypatch.setattr(cli, '_load_live_settings', forbidden)
    base = ['--mode','live_isolated','--allow-live','--provider','groq','--disable-telegram-transport']
    assert cli.main([*base,'--max-provider-calls','16']) == 3
    assert not calls
    assert cli.main([*base,'--max-provider-calls','20']) == 3
    assert calls == ['settings']
    assert 'partial_coverage_risk=true' in capsys.readouterr().out
    assert budget_coverage() == dict(minimum=17, estimated_normal=25,
        conservative_without_fallback=34, conservative_with_fallback=68)
