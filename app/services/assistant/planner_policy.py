"""Process-local admission and physical attempts; no business state or identities."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from time import monotonic
from uuid import uuid4

from app.services.assistant.providers import ProviderError, ProviderErrorKind

logger = logging.getLogger(__name__)


@dataclass
class PlannerRequest:
    correlation: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0
    used_providers: set[str] = field(default_factory=set)
    boundary_crossed: bool = False
    provider_allowed: bool = True
    deadline: float | None = None


CURRENT_REQUEST: ContextVar[PlannerRequest | None] = ContextVar("planner_request", default=None)
ATTEMPT_ROLE: ContextVar[tuple[str, str | None]] = ContextVar("planner_attempt_role", default=("primary", None))


@contextmanager
def planner_request(*, fresh=False, provider_allowed=True):
    existing = CURRENT_REQUEST.get()
    if existing is not None and not fresh:
        yield existing
        return
    state = PlannerRequest(provider_allowed=provider_allowed)
    token = CURRENT_REQUEST.set(state)
    try:
        yield state
    finally:
        CURRENT_REQUEST.reset(token)


def mark_tool_boundary():
    """Any executed tool forbids subsequent provider fallback, including reads."""
    if (state := CURRENT_REQUEST.get()) is not None:
        state.boundary_crossed = True


def mutation_evidence(text):
    """Questions/explanations are not authority, even if a model labels them so."""
    import re
    from app.services.assistant.bar_language import normalize_bar_text, parse_bar_request
    if parse_bar_request(text).mutation:
        return True
    return bool(re.match(
        r"^(?:пожалуйста\s+)?(?:измени|изменить|добавь|добавить|удали|удалить|убери|"
        r"замени|заменить|закрой|закрыть|открой|открыть|создай|создать|отправь|"
        r"отправить|настрой|настроить|отмени|отменить|подтверди|подтвердить)\b",
        normalize_bar_text(text),
    ))


class PlannerBusy(ProviderError):
    def __init__(self):
        super().__init__("planner", ProviderErrorKind.BUSY)


class PlannerAttemptPolicy:
    """Immediate rejection, zero queue/tasks, shared across actors and providers.

    Minimum intervals are start-to-start. Each logical request has at most two
    physical attempts and at most one per provider, across both tool-loop steps.
    A permitted first read is retained when no further physical slot is available.
    """

    def __init__(self, *, intervals=None, cooldowns=None, clock=monotonic, sink=None):
        self.intervals = intervals or {"gemini": 10., "groq": 15.}
        self.cooldowns = cooldowns or {"gemini": 90., "groq": 90.}
        if any(v < 0 for v in (*self.intervals.values(), *self.cooldowns.values())):
            raise ValueError("invalid planner policy limits")
        self.clock = clock
        self.next_start = {"gemini": 0., "groq": 0.}
        self.cooldown_until = {"gemini": 0., "groq": 0.}
        self.active = False
        self.sink = sink or self._log

    @staticmethod
    def _log(row):
        logger.info("assistant_planner_attempt %s", json.dumps(row, separators=(",", ":")))

    def acquire(self, provider):
        state = CURRENT_REQUEST.get()
        if state is None:
            raise PlannerBusy()
        now = self.clock()
        if (not state.provider_allowed or self.active or now < self.next_start[provider]
            or now < self.cooldown_until[provider] or state.attempts >= 2
            or provider in state.used_providers
            or (state.deadline is not None and now >= state.deadline)):
            raise PlannerBusy()
        self.active = True
        self.next_start[provider] = now + self.intervals[provider]
        state.attempts += 1
        state.used_providers.add(provider)
        alias, reason = ATTEMPT_ROLE.get()
        return dict(correlation=state.correlation, provider=provider, alias=alias,
            ordinal=state.attempts, category="internal", latency_ms=0, validation="not_completed",
            fallback_reason=reason, circuit="closed", cooldown="inactive",
            physical_attempt_count=state.attempts)

    def finish(self, provider, row, *, status=None, retry_after=None):
        if status == 429:
            # Never trust unbounded, negative, NaN or infinite Retry-After.
            import math
            delay = retry_after if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) else 0
            delay = max(self.cooldowns[provider], min(900., max(0., delay)))
            self.cooldown_until[provider] = self.clock() + delay
            row["cooldown"] = "active"
        self.active = False  # No await: cancellation cannot strand the permit.

    def emit(self, row):
        try:
            self.sink(dict(row))
        except Exception:
            logger.warning("assistant_planner_attempt_telemetry_failed")


class GovernedPlannerTransport:
    """Count precisely at HTTP boundary, without changing prompts or schemas."""

    def __init__(self, transport, policy, *, transport_trace=False):
        self.transport, self.policy = transport, policy
        self._last_row = ContextVar('planner_transport_attempt', default=None)
        self.transport_trace = transport_trace

    @property
    def last_row(self):
        return self._last_row.get()

    @last_row.setter
    def last_row(self, row):
        self._last_row.set(row)

    async def post_json(self, url, **kwargs):
        with planner_request():
            return await self._post_json(url, **kwargs)

    async def _post_json(self, url, **kwargs):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.query or parsed.username or parsed.password:
            raise ValueError("invalid planner endpoint")
        if parsed.netloc == "api.groq.com" and parsed.path == "/openai/v1/chat/completions":
            provider = "groq"
        elif parsed.netloc == "generativelanguage.googleapis.com" and parsed.path.startswith("/v1beta/models/") and parsed.path.endswith(":generateContent"):
            provider = "gemini"
        else:
            raise ValueError("invalid planner endpoint")
        self.last_row = None
        row = self.policy.acquire(provider)
        self.last_row = row
        started = self.policy.clock()
        response = None
        try:
            response = await self.transport.post_json(url, **kwargs)
            row["category"] = ("success" if 200 <= response.status < 300 else
                "rate_limit" if response.status == 429 else "server_error" if response.status >= 500 else "http_error")
            return response
        except asyncio.CancelledError:
            row["category"] = "cancelled"
            raise
        except TimeoutError:
            row["category"] = "timeout"
            raise
        except Exception:
            row["category"] = "network"
            raise
        finally:
            row["latency_ms"] = max(0, round((self.policy.clock()-started)*1000))
            self.policy.finish(provider, row, status=response.status if response else None,
                retry_after=getattr(response, "retry_after_seconds", None))
            if self.transport_trace:
                row['validation'] = 'transport_only'
                self.policy.emit(row)
                self.last_row = None
