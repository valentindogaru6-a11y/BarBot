"""Evaluation-only physical HTTP budget; never installed in production."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from contextvars import ContextVar
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit


class EvaluationSafetyError(BaseException):
    """Hard stop that must not be swallowed by provider/tool fallback handlers."""

    def __init__(self, category, *, effects=0, operations=0):
        super().__init__(category)
        self.effects, self.operations = effects, operations


class ProviderCallBudgetExhausted(BaseException):
    """Run-level control signal, deliberately bypassing adapter Exception catches."""


CALL_ROLE = ContextVar('evaluation_provider_role', default='primary')
CALL_STAGE = ContextVar('evaluation_provider_stage', default='')
CALL_RETRY = ContextVar('evaluation_provider_retry', default=0)


class PhysicalCallBudget:
    def __init__(self, maximum: int):
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ValueError('max-provider-calls must be between 1 and 1000')
        self.maximum = maximum
        self._lock = Lock()
        self.rows: list[dict] = []
        self.denied = 0

    def acquire(self, provider: str, model: str, stage: str) -> dict:
        if (provider not in ('groq','gemini') or stage not in ('stage_1','stage_2','summary','memory','other')
            or CALL_ROLE.get() not in ('primary','fallback') or type(CALL_RETRY.get()) is not int
            or not 0 <= CALL_RETRY.get() <= 1000):
            raise EvaluationSafetyError('invalid_call_metadata')
        with self._lock:
            if len(self.rows) >= self.maximum:
                self.denied += 1
                raise ProviderCallBudgetExhausted('provider_call_budget_exhausted')
            row = dict(provider=provider, model=model, stage=stage, role=CALL_ROLE.get(),
                       retry=int(CALL_RETRY.get()), status=None, category='in_flight',
                       latency_ms=0, request_bytes=0, response_bytes=0, tokens={})
            self.rows.append(row)
            return row

    def report(self) -> dict:
        with self._lock:
            rows = [dict(row) for row in self.rows]
        failed = sum(row['category'] != 'success' for row in rows)
        return dict(maximum=self.maximum, attempted=len(rows), completed=len(rows)-failed,
                    failed=failed, denied=self.denied,
                    by_provider=dict(Counter(row['provider'] for row in rows)),
                    by_model=dict(Counter(row['model'] for row in rows)),
                    by_stage=dict(Counter(row['stage'] for row in rows)),
                    by_category=dict(Counter(row['category'] for row in rows)),
                    fallback_count=sum(row['role'] == 'fallback' for row in rows),
                    retry_count=sum(row['retry'] > 0 for row in rows), calls=rows)


class BudgetedEvaluationTransport:
    """The sole network boundary for every evaluation provider adapter."""

    def __init__(self, transport, budget: PhysicalCallBudget):
        self.transport, self.budget = transport, budget

    async def post_json(self, url, *, headers, payload, **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query:
            raise EvaluationSafetyError('provider_endpoint_denied')
        if parsed.netloc == 'api.groq.com' and parsed.path == '/openai/v1/chat/completions':
            provider, model = 'groq', payload.get('model', '')
        elif parsed.netloc == 'generativelanguage.googleapis.com' and parsed.path.startswith('/v1beta/models/') and parsed.path.endswith(':generateContent'):
            provider = 'gemini'
            model = parsed.path.removeprefix('/v1beta/models/').removesuffix(':generateContent')
        else:
            raise EvaluationSafetyError('provider_endpoint_denied')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,100}', model):
            raise EvaluationSafetyError('invalid_model_metadata')
        schema = payload.get('response_format', {}).get('json_schema', {})
        stage = CALL_STAGE.get() or ('stage_2' if 'argument' in str(schema.get('name', '')).lower() else 'stage_1')
        row = self.budget.acquire(provider, model, stage)
        row['request_bytes'] = len(json.dumps(payload, ensure_ascii=False).encode())
        started = monotonic()
        try:
            response = await self.transport.post_json(url, headers=headers, payload=payload, **kwargs)
            from app.services.assistant.evaluation_observer import PROVIDER_STEP
            if (step := PROVIDER_STEP.get()) is not None and 200 <= response.status < 300:
                step.inspect_response(provider, response.body)
            row['status'] = response.status
            row['response_bytes'] = len(response.body)
            row['category'] = ('success' if 200 <= response.status < 300 else
                               'auth' if response.status in (401,403) else
                               'rate_limit' if response.status == 429 else
                               'server' if response.status >= 500 else 'invalid_request')
            try:
                body = json.loads(response.body)
                usage = body.get('usage', body.get('usageMetadata', {}))
                row['tokens'] = {key: usage[key] for key in (
                    'prompt_tokens','completion_tokens','total_tokens',
                    'promptTokenCount','candidatesTokenCount','totalTokenCount'
                ) if type(usage.get(key)) is int and 0 <= usage[key] <= 10_000_000}
            except (ValueError, TypeError, AttributeError):
                pass
            return response
        except asyncio.CancelledError:
            row['category'] = 'cancelled'
            raise
        except TimeoutError:
            row['category'] = 'timeout'
            raise
        except Exception:
            row['category'] = 'connection'
            raise
        finally:
            row['latency_ms'] = round((monotonic()-started)*1000, 3)

    async def post_multipart(self, *args, **kwargs):
        raise EvaluationSafetyError('non_llm_transport_denied')


class ObservedPlannerProvider:
    """Observe canonical adapter validation, not provider bodies or prompts."""

    def __init__(self, provider, role: str, *, observer=None, registered_capabilities=()):
        from app.services.assistant.evaluation_observer import EvaluationAuthorizationObserver
        self.provider = provider
        self.provider_name, self.model = provider.provider_name, provider.model
        self.role, self.results = role, []
        self.observer = observer or EvaluationAuthorizationObserver()
        self.registered_capabilities = frozenset(registered_capabilities)

    async def plan(self, snapshot, text, capabilities):
        from app.services.assistant.evaluation_observer import PROVIDER_STEP, ProviderStepObservation
        from app.services.assistant.planner_providers import planner_target_options
        available = frozenset(cap.capability_id for cap in capabilities)
        targets = planner_target_options(snapshot)
        step = ProviderStepObservation(available, self.registered_capabilities or available,
            len(targets), self.observer, tuple(target.domain for target in targets))
        step_token = PROVIDER_STEP.set(step)
        token = CALL_ROLE.set(self.role)
        try:
            result = await self.provider.plan(snapshot, text, capabilities)
            self.results.append(dict(provider=self.provider_name, model=self.model,
                validation=result.validation_status.value,
                error=result.error.value if result.error else None, failures=step.failures(result)))
            return result
        finally:
            CALL_ROLE.reset(token)
            PROVIDER_STEP.reset(step_token)
