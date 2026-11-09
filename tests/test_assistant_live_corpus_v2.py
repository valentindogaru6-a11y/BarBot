"""Offline evidence only, not live conversational quality."""

import asyncio
import hashlib
import json
import subprocess

import pytest

from app.services.assistant.evaluation_corpus import LIVE_CORPUS, LiveInput, LiveExpectation
from app.services.assistant.evaluation_corpus_v2 import select_suite, VARIANTS
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.evaluation_budget import PhysicalCallBudget, BudgetedEvaluationTransport
from app.services.assistant.evaluation_scenarios import ProbeLimitedTransport, PROBE_CALLS, ProbeComplete
from app.services.assistant.live_evaluation import LiveConversationEvaluationAdapter, budget_coverage, score_observation, run_live, suite_thresholds_pass
from scripts import evaluate_assistant_conversations as cli
from tests.test_assistant_live_evaluation import Wire, route, planner, options, settings


@pytest.mark.parametrize('name,dialogues,messages,lifecycle',[
    ('smoke',5,20,8),('core',9,51,18),('extended',19,146,37),
])
def test_counts_and_unique_ids(name,dialogues,messages,lifecycle):
    suite,_ = select_suite(name)
    assert len({c.request.case_id for c in suite.cases})==len(suite.cases)
    assert len({c.request.dialogue for c in suite.cases})==dialogues
    assert sum(c.request.action=='message' for c in suite.cases)==messages
    assert sum(c.request.action!='message' for c in suite.cases)==lifecycle
    if name=='smoke':
        assert suite.cases is LIVE_CORPUS
        assert select_suite('smoke',991)[0].cases is LIVE_CORPUS
    else:
        assert all(any(c.request.dialogue==s and c.request.action=='begin_dialogue' for c in suite.cases)
                   for s in {c.request.dialogue for c in suite.cases})


def test_v1_contract_unchanged():
    fields=('capabilities','dialogue_acts','domain','target','clarification','preview','operations',
            'memory_active','restart','argument_ok','reference','safety')
    def value(corpus):
        return [(c.request.case_id,c.request.dialogue,c.request.text,c.request.action,
                {k:getattr(c.expected,k) for k in fields}) for c in corpus]
    original=subprocess.check_output(['git','show','bd73be9:app/services/assistant/evaluation_corpus.py'],text=True)
    namespace={}
    exec(compile(original,'frozen-v1','exec'),namespace)
    assert value(LIVE_CORPUS)==value(namespace['LIVE_CORPUS'])


def test_deterministic_variants_and_unchanged_expectations():
    one,ids=select_suite('extended',42)
    assert (one,ids)==select_suite('extended',42)
    assert select_suite('extended',43)[1]!=ids
    base,_=select_suite('extended',0)
    assert [c.expected for c in one.cases]==[c.expected for c in base.cases]
    seen={key:set() for key in VARIANTS}
    for seed in range(300):
        suite,_=select_suite('extended',seed)
        for case in suite.cases:
            if case.request.case_id in seen:
                seen[case.request.case_id].add(case.request.text)
    assert all(seen[key]==set(values) for key,values in VARIANTS.items())


def test_static_estimates_exclude_lifecycle():
    assert budget_coverage()==dict(minimum=17,estimated_normal=25,
        conservative_without_fallback=34,conservative_with_fallback=68)
    for name in ('core','extended'):
        suite,_=select_suite(name)
        e=budget_coverage(suite.cases)
        assert 0<e['minimum']<=e['estimated_normal']<=e['conservative_without_fallback']<=e['conservative_with_fallback']
        assert budget_coverage(tuple(c for c in suite.cases if c.request.action=='message'))==e
    assert all(budget_coverage(select_suite('core',seed)[0].cases)['minimum']==48 for seed in range(25))


@pytest.mark.parametrize('name',['core','extended'])
def test_cli_estimate_before_keys(monkeypatch,capsys,name):
    monkeypatch.setattr(cli,'_load_live_settings',lambda:pytest.fail('key access'))
    assert cli.main(['--mode','live_isolated','--allow-live','--provider','groq','--suite',name,
        '--variant-seed','7','--max-provider-calls','1','--disable-telegram-transport'])==3
    output=capsys.readouterr().out
    assert 'static_budget_estimate suite='+name in output
    assert 'provider_budget_below_corpus_minimum' in output


@pytest.mark.parametrize('args',[['--suite','core'],['--variant-seed','0'],['--suite','unknown']])
def test_offline_rejects_live_controls(args):
    assert cli.main(args)==3


def test_real_fixtures_and_reopen():
    with IsolatedEvaluationEnvironment() as env:
        assert env.state()['attendance_open']==1
        env.configure('extended')
        root=env.root
        assert env.state()['attendance_open']==2
        assert len(env.operations.repository.products())==6
        assert len(env.recipients.list_recipients(actor_user_id=1))==2
        assert len(env.closing.list_rules(actor_user_id=1))==2
        assert len(env.schedules.list(1))==1
        assert list((env.root/'reports').rglob('*.xlsx'))
        before=env.state()
        env.recreate()
        assert env.state()==before
        assert not env.policy.can_enter_assistant(12,chat_type='private')
        assert env.policy.can_enter_assistant(11,chat_type='private')
    assert not root.exists()


def test_scene_restart_isolation_denial_and_reset():
    async def run():
        wire=Wire(route('catalog.search','catalog',action='search'),route())
        with IsolatedEvaluationEnvironment() as env:
            env.configure('core')
            adapter=LiveConversationEvaluationAdapter(env,planner(wire,PhysicalCallBudget(20)))
            await adapter.evaluate(LiveInput('begin','X',action='begin_dialogue'))
            first=await adapter.evaluate(LiveInput('read','X','найди пф для облепихи'))
            assert first['target']=='sea'
            restarted=await adapter.evaluate(LiveInput('restart','X',action='restart'))
            assert restarted['restart'] and restarted['context_target']=='sea'
            other=await adapter.evaluate(LiveInput('other','X','Что входит в неё?',actor='other'))
            assert other['context_target']=='absent'
            assert env.memory.get(1,1).last_successful_entity_id.endswith('/sea')
            before=len(wire.requests)
            for req in (LiveInput('blocked','X','Возможности',actor='blocked'),
                        LiveInput('group','X','Возможности',chat_type='group')):
                row=await adapter.evaluate(req)
                assert row['denied'] and row['tools']==0
            await adapter.evaluate(LiveInput('revoke','X',action='revoke_pilot'))
            denied=await adapter.evaluate(LiveInput('stale','X',action='confirm_proposal'))
            assert denied['denied'] and len(wire.requests)==before
            await adapter.evaluate(LiveInput('restore','X',action='restore_pilot'))
            await adapter.evaluate(LiveInput('reset','X',action='reset_context'))
            assert env.memory.get(1,1) is None
    asyncio.run(run())


@pytest.mark.parametrize('fault',['timeout','malformed_second','rate_limit','fallback_disabled','budget_exhausted'])
def test_fault_is_synthetic_and_preserves_context(fault):
    async def run():
        wire=Wire()
        with IsolatedEvaluationEnvironment() as env:
            env.configure('core')
            env.memory.remember(1,chat_id=1,last_intent='catalog.get',last_entity_type='catalog',
                last_query='Облепиха',selected_entity_id='preparations/_/sea',last_entity_label='Облепиха')
            adapter=LiveConversationEvaluationAdapter(env,planner(wire,PhysicalCallBudget(4)))
            row=await adapter.evaluate(LiveInput('fault','T','Покажи состав.',fault=fault))
            assert row['injected_fault']==fault and row['fault_observed']
            assert row['context_target']=='sea' and row['claim_released']
            assert row['operations']==0 and not row['skipped'] and not wire.requests
            assert row['synthetic_provider_calls']==(2 if fault=='malformed_second' else 1)
            await adapter.evaluate(LiveInput('next','T','Покажи её снова.'))
            assert wire.requests
    asyncio.run(run())


def test_long_context_summary_restart():
    async def run():
        with IsolatedEvaluationEnvironment() as env:
            env.configure('core')
            adapter=LiveConversationEvaluationAdapter(env,planner(Wire(),PhysicalCallBudget(50)))
            for i in range(22):
                row=await adapter.evaluate(LiveInput(f'm{i}','M',f'Барный вопрос номер {i}'))
                if i==10:
                    restarted=await adapter.evaluate(LiveInput('restart','M',action='restart'))
                    assert restarted['restart']
            assert row['context_turns']==12 and row['summary_present']
            assert row['long_context'] and row['snapshot_bytes']<=24000
    asyncio.run(run())


def test_real_argument_observation_and_duplicate_confirm():
    async def run():
        with IsolatedEvaluationEnvironment() as env:
            env.configure('core')
            wire=Wire(route('warehouse.prepare_writeoff','warehouse',action='create'))
            adapter=LiveConversationEvaluationAdapter(env,planner(wire,PhysicalCallBudget(4)))
            await adapter.evaluate(LiveInput('begin','I',action='begin_dialogue'))
            row=await adapter.evaluate(LiveInput('write','I','спиши апельсин 150 г (испортился)'))
            assert row['quantity_base']=='0.15' and row['operation_delta']==0
            assert row['comment_hash']==hashlib.sha256('испортился'.encode()).hexdigest()[:16]
            for i in range(2):
                row=await adapter.evaluate(LiveInput(f'confirm{i}','I',action='confirm_proposal'))
                assert row['operation_delta']==1
            row=await adapter.evaluate(LiveInput('new','J',action='begin_dialogue'))
            assert row['operation_delta']==0 and row['operations']==1
    asyncio.run(run())


@pytest.mark.parametrize('field,wanted,metric',[
    ('topic_return',True,'topic_return'),('long_context',True,'long_context_persistence'),
    ('denied',True,'isolation'),('state_unchanged',True,'state_preservation'),
    ('response_required',True,'response_present'),('context_target','tea','context_preservation'),
    ('quantity_base','0.15','argument_extraction'),('destination','kitchen','argument_extraction'),
    ('comment_hash','hash','argument_extraction'),
])
def test_metric_missing_actual_fails(field,wanted,metric):
    actual=dict(safety=True,operations=0,domain=None,capability=None,dialogue_act=None,target=None,
        clarification=False,preview=False,memory_active=0,restart=None,argument_ok=None)
    assert score_observation(actual,LiveExpectation(**{field:wanted}))[metric] is False


def test_ordinal_and_correction_scoring():
    actual=dict(safety=True,operations=0,domain='catalog',capability='catalog.get',dialogue_act='correction',
        target='berry',clarification=False,preview=False,memory_active=0,restart=None,argument_ok=None,
        prior_candidates=['tea','berry'],state_unchanged=True)
    expected=LiveExpectation(target_position=2,dialogue_acts=('correction',),state_unchanged=True)
    assert all(score_observation(actual,expected).values())
    assert not score_observation({**actual,'target':'tea'},expected)['entity_resolution']


@pytest.mark.parametrize('suite',['core','extended'])
def test_fake_suite_cannot_claim_quality_and_report_private(suite):
    wire=Wire()
    report,code=asyncio.run(run_live(options(suite=suite,max_provider_calls=600),settings_loader=settings,
        transport_factory=lambda:wire))
    assert code==1 and not report['ready'] and report['suite']==suite
    assert report['lifecycle_actions']>0 and report['telegram_transport_calls']==0
    assert report['required_case_ids']==[c.request.case_id for c in select_suite(suite)[0].cases]
    assert report['call_budget']['attempted']==len(wire.requests)
    assert report['call_budget']['retry_count']==0
    encoded=json.dumps(report)
    for sensitive in ('synthetic-key','first_name','reasoning','PRIVATE_REASONING_MARKER','/opt/barbot','/tmp/',
                      'Запомни,','испортился','Апельсиновый'):
        assert sensitive not in encoded
    if suite=='extended':
        assert set(report['domain_metrics'])=={'catalog','warehouse','attendance','employees','reports','administration','general','memory'}
        assert sum(row.get('synthetic_provider_calls',0) for row in report['rows'])==6


def test_probe_limit_applies_to_any_physical_call():
    async def run():
        wire=Wire()
        budget=PhysicalCallBudget(5)
        transport=ProbeLimitedTransport(BudgetedEvaluationTransport(wire,budget))
        token=PROBE_CALLS.set([0])
        try:
            await transport.post_json('https://api.groq.com/openai/v1/chat/completions',headers={},payload={'model':'synthetic'})
            with pytest.raises(ProbeComplete):
                await transport.post_json('https://generativelanguage.googleapis.com/v1beta/models/synthetic:generateContent',headers={},payload={})
        finally:
            PROBE_CALLS.reset(token)
        assert len(wire.requests)==budget.report()['attempted']==1
    asyncio.run(run())


def test_per_suite_thresholds_missing_metrics_and_weak_domain_cannot_pass():
    from app.services.assistant.evaluation_domain_scoring import required_domain_metrics,SAFETY_COUNTERS
    suite,_=select_suite('extended')
    metrics={name:1.0 for name,_ in suite.thresholds}
    domains={domain:{name:1.0 for name in names} for domain,names in required_domain_metrics(suite).items()}
    safety={name:0 for name in SAFETY_COUNTERS}
    assert suite_thresholds_pass(suite,metrics,domains,safety)
    assert not suite_thresholds_pass(suite,{**metrics,'route':None},domains,safety)
    assert not suite_thresholds_pass(suite,{**metrics,'route':float('nan')},domains,safety)
    assert not suite_thresholds_pass(suite,metrics,{**domains,'reports':{'route':0.0,'proposal':1.0}},safety)
    assert not suite_thresholds_pass(suite,metrics,{**domains,'general':{}},safety)


def test_v2_expected_never_enters_provider_and_partial_budget_blocks_readiness():
    from dataclasses import replace
    corpus=(replace(select_suite('core')[0].cases[1],expected=LiveExpectation(target='SCORER_ONLY_MARKER')),)
    wire=Wire(route('catalog.search','catalog',action='search'),route())
    report,code=asyncio.run(run_live(options(suite='core'),settings_loader=settings,
        transport_factory=lambda:wire,corpus=corpus))
    assert code==1 and not report['ready']
    assert 'SCORER_ONLY_MARKER' not in json.dumps(wire.requests)
    assert 'expected' not in json.dumps(wire.requests)
    class TwoReadWire(Wire):
        async def post_json(self,*args,**kwargs):
            # Local clarification now avoids HTTP. Explicitly exercise two-step
            # reads instead of assuming every incomplete mutation consumes HTTP.
            data=json.loads(kwargs['payload']['messages'][1]['content'])
            allowed={item['capability_id'] for item in data['capabilities']}
            self.responses.append(route('catalog.search','catalog',action='search')
                if 'catalog.search' in allowed else route())
            return await super().post_json(*args,**kwargs)
    wire=TwoReadWire()
    report,code=asyncio.run(run_live(options(suite='core',max_provider_calls=48),settings_loader=settings,
        transport_factory=lambda:wire))
    assert code==3 and not report['ready'] and report['skipped']>0
    assert report['call_budget']['attempted']==48


@pytest.mark.parametrize('error',[RuntimeError('synthetic'),asyncio.CancelledError()])
def test_v2_cleanup_on_provider_error_and_cancellation(error):
    roots=[]
    class Tracked(IsolatedEvaluationEnvironment):
        def __enter__(self):
            result=super().__enter__();roots.append(self.root);return result
    async def run():
        with Tracked() as env:
            env.configure('extended')
            adapter=LiveConversationEvaluationAdapter(env,planner(Wire(error=error),PhysicalCallBudget(10)))
            try:
                await adapter.evaluate(LiveInput('read','X','найди пф для облепихи'))
            finally:
                async with adapter.coordinator.claim(1) as acquired:
                    assert acquired
    if isinstance(error,asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):asyncio.run(run())
    else:
        asyncio.run(run())
    assert roots and not roots[0].exists()
