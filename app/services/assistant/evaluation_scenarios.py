"""Synthetic lifecycle/observations. Never receives ground truth or real IDs."""

from decimal import Decimal
from contextvars import ContextVar
from contextlib import closing
import hashlib
import json
import sqlite3

from app.services.assistant.evaluation_budget import ProviderCallBudgetExhausted
from app.services.assistant.providers import HttpResponse
from app.services.assistant.evaluation_fixtures import protected_fingerprint
from app.services.warehouse.quantity_service import SCALE


class ProbeComplete(BaseException):
    """Stop a single-call probe without pretending that a provider replied."""


PROBE_CALLS = ContextVar('evaluation_probe_calls',default=None)


class ProbeLimitedTransport:
    def __init__(self, transport):
        self.transport = transport

    async def post_json(self,*args,**kwargs):
        state = PROBE_CALLS.get()
        if state is not None:
            if state[0] >= 1:
                raise ProbeComplete
            state[0] += 1
        return await self.transport.post_json(*args,**kwargs)


class FaultTransport:
    """Entirely synthetic fault turn; never delegates to a network transport.

    The second-step fixture uses an actual read on the selected temp card before
    supplying malformed wire. This is continuity evidence, not provider quality.
    """
    def __init__(self, fault):
        self.fault, self.calls, self.triggered = fault, 0, False

    async def post_json(self, url, **kwargs):
        self.calls += 1
        if self.fault == 'malformed_second' and self.calls == 1:
            decision = dict(dialogue_act='follow_up',domain='catalog',capability_id='catalog.get',
                target_index=0,action_code='open',needs_clarification=False,confidence_scaled=10000)
            return HttpResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(decision)}}]}).encode())
        self.triggered = True
        if self.fault in ('timeout','fallback_disabled'):
            raise TimeoutError
        if self.fault == 'budget_exhausted':
            raise ProviderCallBudgetExhausted
        if self.fault == 'rate_limit':
            return HttpResponse(429,b'{}')
        if self.fault == 'malformed_second':
            return HttpResponse(200,b'{')
        raise ValueError('invalid_synthetic_fault')


def capture(e):
    context = e.memory.get(e.owner,e.chat)
    return dict(context_target=(context.last_successful_entity_id if context else ''),
        candidates=tuple(context.current_candidates if context else ()),
        turn_count=len(context.turns) if context else 0,
        context_chars=sum(len(turn.text) for turn in context.turns) if context else 0,
        summary_present=bool(context and context.summary_text),
        protected=protected_fingerprint(e))


def arguments(e):
    """Read actual latest line, including after confirmation; no raw text output."""
    e.validate_path(e.path)
    with closing(sqlite3.connect(e.path)) as connection:
        connection.execute('PRAGMA query_only=ON')
        row = connection.execute('''SELECT l.quantity_scaled,l.comment_text,l.reason_text,d.normalized_name
            FROM warehouse_intake_lines l JOIN warehouse_intake_batches b ON b.id=l.batch_id
            LEFT JOIN warehouse_locations d ON d.id=l.destination_location_id
            WHERE b.actor_user_id=? ORDER BY b.id DESC,l.line_number LIMIT 1''',(e.owner,)).fetchone()
    if row is None:
        return dict(quantity_base=None,destination=None,comment_hash=None)
    comment = ' '.join(str(row[1] or row[2] or '').split())
    return dict(quantity_base=str(Decimal(row[0])/SCALE) if row[0] is not None else None,
        destination={'кухня':'kitchen','кондитерка':'confectionery'}.get(row[3],row[3]),
        comment_hash=hashlib.sha256(comment.encode()).hexdigest()[:16] if comment else None)
