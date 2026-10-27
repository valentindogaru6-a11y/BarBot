"""Closed, privacy-safe evaluation observations; no production instrumentation."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from enum import StrEnum

from app.services.assistant.evaluation_budget import EvaluationSafetyError


class FailureCategory(StrEnum):
    TRANSPORT = 'provider_transport_errors'
    CONTRACT = 'provider_contract_failures'
    WIRE = 'wire_validation_failures'
    CANONICAL = 'canonical_validation_failures'
    UNKNOWN = 'unknown_capability_failures'
    TARGET = 'target_validation_failures'
    SAFETY = 'safety_contract_failures'
    SEMANTIC = 'semantic_mismatches'
    BUDGET = 'budget_skips'


def failure_counts():
    return {category.value: 0 for category in FailureCategory}


@dataclass
class AuthorizationObservation:
    requested_capability: str
    registry_resolution: bool
    access_authorized: bool = False
    risk_authorized: bool = False
    executor_invoked: bool = False
    side_effect_observed: bool = False
    denial_category: str | None = None


class EvaluationAuthorizationObserver:
    def __init__(self):
        self.events: list[AuthorizationObservation] = []

    def begin(self, identifier, *, registered):
        event = AuthorizationObservation(identifier if registered else 'unknown', registered)
        self.events.append(event)
        return event

    def invoke(self, event, executor):
        # Record the actual invocation, including deliberately defective test
        # dispatchers. This is observation, not a replacement permission gate.
        event.executor_invoked = True
        try:
            return executor()
        finally:
            if not (event.registry_resolution and event.access_authorized and event.risk_authorized):
                event.denial_category = 'unauthorized_execution'
                raise EvaluationSafetyError('unauthorized_execution')

    def snapshot(self, start=0):
        return [asdict(event) for event in self.events[start:]]

    def counts(self):
        attempts = sum(not (e.registry_resolution and e.access_authorized and e.risk_authorized)
                       for e in self.events)
        executions = sum(e.executor_invoked and not (
            e.registry_resolution and e.access_authorized and e.risk_authorized) for e in self.events)
        return attempts, executions


class EvaluationDispatchDriver:
    def dispatch(self, event, authorize, execute, observer):
        authorize(event)
        return observer.invoke(event, execute)


@dataclass
class ProviderStepObservation:
    allowed_capabilities: frozenset[str]
    registered_capabilities: frozenset[str]
    target_count: int
    authorization: EvaluationAuthorizationObserver
    target_domains: tuple[str, ...] = ()
    unknown: bool = False
    unavailable: bool = False
    invalid_target: bool = False

    def inspect_response(self, provider, body):
        """Classify only closed contract facts; never retain raw provider data."""
        try:
            envelope = json.loads(body)
            if provider == 'groq':
                content = envelope['choices'][0]['message']['content']
            else:
                content = ''.join(part.get('text', '') for part in
                                  envelope['candidates'][0]['content']['parts'])
            route = json.loads(content)
            identifier = route.get('capability_id')
            if isinstance(identifier, str) and identifier != 'none':
                self.unknown = identifier not in self.registered_capabilities
                self.unavailable = identifier not in self.allowed_capabilities
                if self.unavailable:
                    event = self.authorization.begin(identifier, registered=not self.unknown)
                    event.denial_category = 'unknown_capability' if self.unknown else 'unavailable_capability'
            index = route.get('target_index')
            self.invalid_target = type(index) is int and (index < -1 or index >= self.target_count)
            if type(index) is int and 0 <= index < len(self.target_domains):
                self.invalid_target = route.get('domain') != self.target_domains[index]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            pass  # The real adapter owns wire/canonical validation.

    def failures(self, result):
        counts = failure_counts()
        validation = result.validation_status.value
        error = result.error.value if result.error else None
        counts[FailureCategory.WIRE] = int(validation == 'wire_invalid')
        counts[FailureCategory.CANONICAL] = int(validation == 'canonical_invalid')
        counts[FailureCategory.UNKNOWN] = int(self.unknown)
        counts[FailureCategory.TARGET] = int(self.invalid_target)
        contract = (validation in ('wire_invalid', 'canonical_invalid', 'policy_rejected') or
                    self.unavailable or self.invalid_target or error in (
                        'invalid_schema', 'generated_output_schema_mismatch', 'unsupported_parameter'))
        counts[FailureCategory.CONTRACT] = int(contract)
        counts[FailureCategory.TRANSPORT] = int(bool(error) and not contract)
        return counts


PROVIDER_STEP: ContextVar[ProviderStepObservation | None] = ContextVar('evaluation_provider_step', default=None)
