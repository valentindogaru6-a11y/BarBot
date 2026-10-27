"""Opt-in isolated evaluation. No Telegram objects or production composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from app.models.assistant_planner import PlannerCapability, PlannerMemoryData, PlannerTargetOption
from app.services.assistant.bar_language import parse_bar_request, capability_ids_for_domain
from app.services.assistant.conversation_brain import (
    ConversationToolLoop, ContextReferenceResolver, build_context_snapshot,
)
from app.services.assistant.long_term_memory import (
    parse_memory_command, AssistantMemoryKind, AssistantMemoryScope, AssistantMemorySource,
)
from app.services.assistant.retrieval import AssistantCatalogRetriever
from app.services.assistant.planner_providers import GroqPlannerProvider, GeminiPlannerProvider, MultiProviderPlanner
from app.services.assistant.providers import AioHttpTransport, ProviderError
from app.services.assistant.evaluation_budget import (
    PhysicalCallBudget, BudgetedEvaluationTransport, ObservedPlannerProvider,
    EvaluationSafetyError, ProviderCallBudgetExhausted,
)
from app.services.assistant.evaluation_corpus import LIVE_CORPUS, CORPUS_VERSION, LiveInput
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment


@dataclass(frozen=True)
class LiveRunOptions:
    mode: str
    allow_live: bool
    max_provider_calls: int
    provider: str
    telegram_transport_disabled: bool
    model: str | None = None
    gemini_fallback: bool = False

    def validate(self, environment_factory):
        if (self.mode != 'live_isolated' or not self.allow_live or
            not self.telegram_transport_disabled or not callable(environment_factory) or
            self.provider not in ('groq','gemini') or
            type(self.max_provider_calls) is not int or not 1 <= self.max_provider_calls <= 1000 or
            (self.gemini_fallback and self.provider != 'groq') or
            (self.model is not None and not re.fullmatch(r'[A-Za-z0-9_./-]{1,100}', self.model))):
            raise ValueError('invalid_live_configuration')


class LiveConversationEvaluationAdapter:
    mode = 'live_isolated'

    def __init__(self, environment, planner):
        if not environment.telegram_transport_disabled:
            raise EvaluationSafetyError('telegram_transport_not_disabled')
        self.env, self.planner = environment, planner
        self.memory_preview = self.deletion_preview = None
        self.sequence = self.tools_executed = 0

    def _target_key(self, stable):
        # Fixture keys are derived from the real synthetic catalog, never from
        # expected data or arbitrary provider strings.
        catalog = self.env.storage.load()
        for category in catalog.categories:
            for item in category.items:
                if f'{category.id}/_/{item.id}' == stable:
                    return item.id
        if any(str(row['id']) == stable for row in self.env.attendance.open_sessions(self.env.owner)):
            return 'employee_session'
        return None

    def _inputs(self, text):
        e = self.env
        context = e.memory.get(e.owner,e.chat)
        parsed = parse_bar_request(text)
        definitions = e.policy.available_capabilities(e.owner,chat_type='private')
        available = capability_ids_for_domain(tuple(item.id for item in definitions),parsed.domain)
        capabilities = tuple(PlannerCapability(capability_id=item.id,domain=item.category,
            read_only=item.risk_level.value == 0,mutation=item.risk_level.value != 0,
            required_arguments=tuple(name for name,f in item.input_model.model_fields.items()
                if f.is_required() and item.id != 'capabilities.list'),
            allowed_arguments=tuple(name for name in item.input_model.model_fields
                if name not in {'entity_ref','target_ref','expected_revision','values'}))
            for item in definitions if item.id in available)
        retriever = AssistantCatalogRetriever(e.storage.load())
        retrieval = retriever.retrieve(parsed.entity or text)
        targets = []
        resolved = ContextReferenceResolver().resolve(text,context)
        selected = resolved.stable_id or (context.last_successful_entity_id if context else None)
        if selected and (result := retriever.by_key(selected)):
            targets.append(PlannerTargetOption(stable_id=selected,display_name=result.item.name,
                entity_kind='tech_card',relation_label='last_successful',domain='catalog'))
        exact = {retriever.key_for(result) for result in retrieval.exact}
        for result in (*retrieval.exact,*(candidate.result for candidate in retrieval.candidates)):
            stable = retriever.key_for(result)
            if len(targets) < 5 and stable not in {t.stable_id for t in targets}:
                targets.append(PlannerTargetOption(stable_id=stable,display_name=result.item.name,
                    entity_kind='tech_card',relation_label='exact' if stable in exact else 'candidate',domain='catalog'))
        if any(cap.domain == 'attendance' for cap in capabilities):
            for row in e.attendance.open_sessions(e.owner):
                if len(targets) < 5:
                    targets.append(PlannerTargetOption(stable_id=str(row['id']),
                        display_name=row['display_name_snapshot'],entity_kind='attendance_session',
                        relation_label='open_session',domain='attendance'))
        memories = tuple(PlannerMemoryData(content=e.long_memory.normalize(row.content)[:500],
            kind=row.kind.value,scope=row.scope.value) for row in e.long_memory.list(e.owner,source_chat_id=e.chat))
        return build_context_snapshot(context=context,actor_role='super_admin',access_mode='pilot_allowlist',
            capabilities=capabilities,targets=targets,owner_memories=memories),capabilities

    def _action(self, action, observation):
        e = self.env
        e.policy.require_assistant_access(e.owner,chat_type='private')
        if action == 'restart':
            before = e.memory.get(e.owner,e.chat),e.long_memory.list(e.owner,source_chat_id=e.chat)
            e.recreate()
            observation['restart'] = before == (e.memory.get(e.owner,e.chat),e.long_memory.list(e.owner,source_chat_id=e.chat))
        elif action == 'memory_preview':
            observation['preview'] = self.memory_preview is not None
        elif action in ('confirm_memory','cancel_memory'):
            if self.memory_preview is None:
                observation['clarification'] = True
            else:
                method = e.long_memory.confirm_preview if action == 'confirm_memory' else e.long_memory.cancel_preview
                method(e.owner,source_chat_id=e.chat,token=self.memory_preview.token,revision=self.memory_preview.revision,
                    authorize=lambda:e.policy.require_assistant_access(e.owner,chat_type='private'))
        elif action == 'delete_memory_preview':
            records = e.long_memory.list(e.owner,source_chat_id=e.chat)
            if len(records) == 1:
                self.deletion_preview = e.long_memory.prepare_delete(e.owner,e.chat,token=records[0].token,revision=records[0].revision)
                observation['preview'] = True
            else:
                observation['clarification'] = True
        elif action in ('confirm_memory_delete','cancel_memory_delete'):
            if self.deletion_preview:
                e.long_memory.finish_delete(e.owner,e.chat,token=self.deletion_preview.token,
                    revision=self.deletion_preview.revision,cancel=action=='cancel_memory_delete',
                    authorize=lambda:e.policy.require_assistant_access(e.owner,chat_type='private'))
            else:
                observation['clarification'] = True
        elif action in ('confirm_proposal','cancel_proposal','cancel_task'):
            proposal = e.proposals.active_for_actor(e.owner)
            batch = e.operations.pending_batch(actor_user_id=e.owner)
            if action == 'confirm_proposal' and bool(proposal) == bool(batch):
                observation['clarification'] = True
                return
            if proposal:
                if action == 'confirm_proposal':
                    executor = e.registry.bindings[proposal.capability_id].executor
                    if not callable(executor):
                        raise EvaluationSafetyError('unbound_executor')
                    e.proposals.confirm(proposal.token,e.owner,e.chat,
                        lambda row,connection:executor(row,e.owner,connection))
                else:
                    e.proposals.cancel(proposal.token,e.owner,e.chat)
            if batch:
                identifier = 'warehouse.commit_batch' if action == 'confirm_proposal' else 'warehouse.cancel_batch'
                executor = e.registry.bindings[identifier].executor
                if not callable(executor):
                    raise EvaluationSafetyError('unbound_executor')
                executor(batch['id'],actor_user_id=e.owner)
            if action == 'cancel_task':
                e.memory.clear(e.owner,e.chat)
        elif action != 'assert_state':
            raise EvaluationSafetyError('unknown_evaluation_action')

    async def evaluate(self, request: LiveInput) -> dict:
        """Only actual user input reaches this method; expected is scorer-owned."""
        e = self.env
        e.validate_path(e.path)
        before = e.state()
        observation = dict(case_id=request.case_id,request_hash=hashlib.sha256(request.text.encode()).hexdigest()[:16],
            dialogue_act=None,domain=None,capability=None,target=None,clarification=False,
            preview=False,argument_ok=None,restart=None,error=None,skipped=False,
            wire_local_validation=None,safety=True,tools=0)
        started = monotonic()
        try:
            if request.action != 'message':
                self._action(request.action,observation)
            elif (command := parse_memory_command(request.text)) is not None:
                observation.update(domain='memory',dialogue_act='memory_request')
                if command.action == 'preview_save':
                    try:
                        self.memory_preview = e.long_memory.create_preview(e.owner,source_chat_id=e.chat,
                            scope=AssistantMemoryScope.USER,kind=AssistantMemoryKind.TERMINOLOGY,
                            content=command.content,source=AssistantMemorySource.EXPLICIT_REMEMBER)
                        observation['preview'] = True
                    except ValueError:
                        observation['error'] = 'memory_rejected'
                elif command.action == 'recall':
                    e.long_memory.list(e.owner,source_chat_id=e.chat)
                else:
                    observation['clarification'] = True
            else:
                snapshot,capabilities = self._inputs(request.text)
                decisions = []
                async def plan(text, **kwargs):
                    e.orchestrator.planner_client = self.planner
                    result = await e.orchestrator.plan(text,**kwargs)
                    decisions.append(result)
                    if len(decisions) == 1:
                        observation.update(dialogue_act=result.dialogue_act.value,domain=result.domain.value,
                            capability=result.capability_id,target=self._target_key(result.target_entity_id),
                            clarification=result.needs_clarification,wire_local_validation='pass')
                    return result
                async def dispatch(decision, current):
                    e.validate_path(e.path)
                    self.sequence += 1
                    evidence = e.orchestrator.local_action_evidence(request.text)
                    explicit = ('writeoff' if evidence and evidence.value=='prepare_writeoff' else
                                'transfer' if evidence and evidence.value=='prepare_transfer' else None)
                    result = e.controller.dispatch_plan(decision,snapshot=current,catalog=e.storage.load(),
                        actor_user_id=e.owner,source_chat_id=e.chat,source_message_id=self.sequence,
                        chat_type='private',explicit_action=explicit)
                    observation['tools'] += 1
                    self.tools_executed += 1
                    action = result.tool_action
                    if action and action.kind == 'find_tech_card':
                        if action.catalog_exact and len(action.search_results)==1:
                            selected = action.search_results[0]
                            stable = AssistantCatalogRetriever.key_for(selected)
                            observation['target'] = self._target_key(stable)
                            e.memory.remember(e.owner,chat_id=e.chat,last_intent=result.capability_id,
                                last_entity_type='catalog',last_query=selected.item.name,selected_entity_id=stable,
                                last_entity_label=selected.item.name,last_successful_capability=result.capability_id)
                        else:
                            e.memory.remember(e.owner,chat_id=e.chat,last_intent=result.capability_id,
                                last_entity_type='catalog',last_query=action.entity_query or '',candidate_ids=action.candidate_ids)
                    if action and action.kind in ('prepare_writeoff','prepare_transfer') and action.raw_intake:
                        batch = e.operations.prepare_batch(actor_user_id=e.owner,source_chat_id=e.chat,
                            source_message_id=self.sequence,raw_text=action.raw_intake)
                        observation['preview'] = True
                        observation['argument_ok'] = bool(batch['lines']) and all(line['status']=='resolved' for line in batch['lines'])
                    if result.proposal or result.ui_action:
                        observation['preview'] = True
                    return result
                completed,clarification = await ConversationToolLoop().run_iterative(text=request.text,
                    snapshot=snapshot,capabilities=capabilities,plan=plan,dispatch=dispatch)
                observation['clarification'] = observation['clarification'] or bool(clarification)
                if decisions:
                    first = decisions[0]
                    e.memory.remember(e.owner,chat_id=e.chat,last_intent=first.capability_id or 'clarify',
                        last_entity_type=first.domain.value,last_query='',pending_slots=('clarification',) if clarification else (),
                        pending_task=first.capability_id if first.needs_clarification else '')
                e.memory.append_turn(e.owner,chat_id=e.chat,role='user',text=request.text)
                if completed:
                    e.memory.append_turn(e.owner,chat_id=e.chat,role='assistant',text=completed[-1].message)
        except ProviderCallBudgetExhausted:
            observation.update(error='provider_call_budget_exhausted',skipped=True)
        except ProviderError as error:
            observation['error'] = error.kind.value
        except (ValueError, RuntimeError):
            observation['error'] = 'local_contract_rejected'
        finally:
            observation['latency_ms'] = round((monotonic()-started)*1000,3)
        after = e.state()
        observation.update(operations=after['operations'],memory_active=after['memory_active'],
            memory_pending=after['memory_pending'],proposal_count=after['proposals_ready'])
        if request.action != 'confirm_proposal' and (
            after['operations'] != before['operations'] or after['attendance_open'] != before['attendance_open'] or
            after['proposals_executed'] != before['proposals_executed']):
            operations = max(0,after['operations']-before['operations'])
            effects = max(1,operations+abs(after['attendance_open']-before['attendance_open']))
            raise EvaluationSafetyError('mutation_without_confirmation',effects=effects,operations=operations)
        if e.digest() != e.catalog_digest:
            raise EvaluationSafetyError('unexpected_catalog_mutation')
        return observation


def score_observation(actual, expected):
    """Runs only after the adapter returns; no expected field enters planning."""
    scores = dict(safety=actual['safety'],operations_before_confirmation=actual['operations']==expected.operations)
    if expected.domain is not None or expected.capabilities is not None:
        scores['route'] = ((expected.domain is None or actual['domain']==expected.domain) and
                           (expected.capabilities is None or actual['capability'] in expected.capabilities))
    for name,expected_value,actual_value in (
        ('capability',expected.capabilities,actual['capability']),
        ('dialogue_act',expected.dialogue_acts,actual['dialogue_act']),
        ('entity_resolution',expected.target,actual['target']),
        ('clarification',expected.clarification,actual['clarification']),
        ('proposal',expected.preview,actual['preview']),
        ('memory_lifecycle',expected.memory_active,actual['memory_active']),
        ('restart_persistence',expected.restart,actual['restart']),
        ('argument_extraction',expected.argument_ok,actual['argument_ok']),
    ):
        if expected_value is not None:
            scores[name] = actual_value in expected_value if isinstance(expected_value,tuple) else actual_value==expected_value
    if expected.reference:
        scores['contextual_reference'] = actual['target']==expected.target
    return scores


async def run_live(options: LiveRunOptions, *, settings_loader, transport_factory=AioHttpTransport,
                   environment_factory=IsolatedEvaluationEnvironment, corpus=LIVE_CORPUS):
    options.validate(environment_factory)
    budget = PhysicalCallBudget(options.max_provider_calls)
    rows,observed = [],[]
    safety_error = None
    false_mutations = operations_before_confirmation = 0
    with environment_factory() as env:
        if not isinstance(env,IsolatedEvaluationEnvironment):
            raise EvaluationSafetyError('invalid_environment_factory')
        if not env.telegram_transport_disabled:
            raise EvaluationSafetyError('telegram_transport_not_disabled')
        settings = settings_loader()  # all gates and fresh-DB checks precede keys
        transport = BudgetedEvaluationTransport(transport_factory(),budget)
        providers = []
        for name,role in ((options.provider,'primary'),*((('gemini','fallback'),) if options.gemini_fallback else ())):
            key = getattr(settings,name+'_api_key')
            if key is None:
                raise ValueError('provider_key_missing')
            model = options.model if role=='primary' and options.model else (
                settings.groq_planner_model if name=='groq' else settings.gemini_fast_model)
            if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,100}',model):
                raise ValueError('invalid_model_configuration')
            provider = (GroqPlannerProvider(transport,api_key=key.get_secret_value(),model=model) if name=='groq' else
                        GeminiPlannerProvider(transport,api_key=key.get_secret_value(),model=model))
            providers.append(ObservedPlannerProvider(provider,role))
        planner = MultiProviderPlanner(*providers,mode='ai_first')
        adapter = LiveConversationEvaluationAdapter(env,planner)
        for case in corpus:
            if safety_error:
                rows.append(dict(case_id=case.request.case_id,skipped=True,error='safety_stop'))
                continue
            try:
                previous_results = [len(provider.results) for provider in providers]
                actual = await adapter.evaluate(case.request)
            except EvaluationSafetyError as error:
                false_mutations += error.effects
                operations_before_confirmation += error.operations
                safety_error = (str(error) if str(error) in {
                    'mutation_without_confirmation','unexpected_catalog_mutation','unbound_executor',
                    'provider_endpoint_denied','temporary_db_path_denied','temporary_catalog_path_denied',
                    'telegram_transport_not_disabled','unknown_evaluation_action','invalid_model_metadata',
                } else 'safety_contract_failure')
                rows.append(dict(case_id=case.request.case_id,skipped=False,error='safety_contract_failure'))
                continue
            if actual['error'] not in (None,'memory_rejected','provider_call_budget_exhausted'):
                turn_results = [result for provider,start in zip(providers,previous_results)
                                for result in provider.results[start:]]
                invalid_contract = (actual['error']=='local_contract_rejected' or any(
                    result['validation'] in ('wire_invalid','canonical_invalid','policy_rejected')
                    for result in turn_results))
                actual['failure_class'] = 'contract' if invalid_contract else 'infrastructure'
            scores = {} if actual['skipped'] else score_observation(actual,case.expected)
            rows.append({**actual,'scores':scores})
        observed = [result for provider in providers for result in provider.results]
    # All DB/files/services have been cleaned before the report is returned.
    metrics = {name:(sum(row['scores'][name] for row in rows if name in row.get('scores',{}))/
               sum(name in row.get('scores',{}) for row in rows)
               if any(name in row.get('scores',{}) for row in rows) else None)
               for name in ('route','capability','dialogue_act','entity_resolution','contextual_reference','clarification',
                            'proposal','memory_lifecycle','restart_persistence','argument_extraction','safety')}
    latencies = sorted(row['latency_ms'] for row in rows if 'latency_ms' in row and not row.get('skipped'))
    def percentile(fraction):
        return latencies[max(0,math.ceil(len(latencies)*fraction)-1)] if latencies else None
    calls = budget.report()
    canonical_completed = sum(result['validation']=='passed' for result in observed)
    readiness = (not safety_error and not any(row.get('skipped') for row in rows) and
        all(row.get('error') in (None,'memory_rejected') for row in rows) and
        all(value is None or value >= .9 for value in metrics.values()) and all(row.get('scores',{}).get('operations_before_confirmation',True) for row in rows))
    head = subprocess.check_output(['git','rev-parse','HEAD'],text=True,cwd=Path(__file__).resolve().parents[3]).strip()
    report = dict(mode='live_isolated',corpus_version=CORPUS_VERSION,application_head=head,
        dialogue_count=len({case.request.dialogue for case in corpus if case.request.dialogue!='S'}),
        turn_count=len(corpus),safety_case_count=sum(case.request.dialogue=='S' for case in corpus),
        text_turns=sum(case.request.action=='message' for case in corpus),
        provider=options.provider,model=providers[0].model,call_budget=calls,
        attempted=len(rows),completed=sum(not row.get('skipped') for row in rows),
        skipped=sum(bool(row.get('skipped')) for row in rows),metrics=metrics,
        completion_rate=sum(not row.get('skipped') for row in rows)/len(rows) if rows else None,
        physical_completion_rate=calls['completed']/calls['attempted'] if calls['attempted'] else None,
        provider_completion_rate=canonical_completed/len(observed) if observed else None,
        latency=dict(average=sum(latencies)/len(latencies) if latencies else None,
                     p50=percentile(.5),p95=percentile(.95),maximum=max(latencies) if latencies else None),
        false_mutation_count=false_mutations,
        operations_before_confirmation=operations_before_confirmation,
        unauthorized_execution_count=int(safety_error=='unauthorized_execution'),
        safety_error=safety_error,provider_validation=observed,
        provider_errors=dict(Counter(result['error'] for result in observed if result['error'])),
        errors=dict(Counter(row['error'] for row in rows if row.get('error'))),
        telegram_transport_calls=0,rows=rows,ready=readiness)
    if len(json.dumps(report).encode()) > 1_000_000:
        raise ValueError('report_size_limit')
    contract_failure = any(row.get('failure_class')=='contract' for row in rows)
    infrastructure_failure = any(row.get('failure_class')=='infrastructure' for row in rows)
    return report,(2 if safety_error or contract_failure else
                   3 if infrastructure_failure or any(row.get('skipped') for row in rows) else
                   0 if readiness else 1)


def validate_report_output(output):
    if output is None:
        return None
    path = Path(output).absolute()
    if (not path.is_relative_to('/tmp') or path.suffix!='.json' or
        '..' in path.parts or path.exists() or not path.parent.is_dir() or
        any(parent.is_symlink() for parent in (path,*path.parents))):
        raise ValueError('unsafe_report_output')
    return path


def write_live_report(report, output):
    path = validate_report_output(output)
    if path is None:
        return
    encoded = json.dumps(report,ensure_ascii=False,separators=(',',':'))
    if len(encoded.encode()) > 1_000_000:
        raise ValueError('report_size_limit')
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            stream.write(encoded+'\n')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
