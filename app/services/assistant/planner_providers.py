"""Provider-neutral planner adapters and bounded sequential fallback policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Protocol

import aiohttp
from pydantic import ValidationError

from app.models.assistant import AssistantDomain
from app.models.assistant_planner import (
    AssistantDialogueAct,
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerContextUpdates,
    PlannerDecision,
)
from app.models.assistant_provider_planner import (
    GeminiPlannerEnvelope,
    GroqPlannerEnvelope,
    PlannerInferredAction,
    PlannerNotesCode,
    PlannerValidationStatus,
    ProviderErrorDiagnostic,
    ProviderPlanError,
    ProviderPlanResult,
    SafeProviderErrorCode,
    SafeProviderErrorType,
)
from app.services.assistant.providers import (
    AssistantHttpTransport,
    HttpResponse,
    MAX_PLANNER_REQUEST_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderError,
    ProviderErrorKind,
    _ResponseTooLargeError,
)
from app.services.assistant.system_prompt import build_assistant_system_prompt

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_PLANNER_SCHEMA_NAME = "barbot_groq_planner_v1"
GEMINI_PLANNER_SCHEMA_NAME = "barbot_gemini_planner_v1"
MAX_ARGUMENT_ITEMS = 20
MAX_ARGUMENT_VALUE_LENGTH = 1_000


class PlannerProvider(Protocol):
    provider_name: str
    model: str

    async def plan(
        self,
        snapshot: PlannerContextSnapshot,
        message: str,
        allowed_capabilities: Sequence[PlannerCapability],
    ) -> ProviderPlanResult: ...


def _nullable_string(*, max_length: int, enum: Sequence[str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": ["string", "null"], "maxLength": max_length}
    if enum is not None:
        result["enum"] = [*enum, None]
    return result


def planner_wire_schema(
    capabilities: Sequence[PlannerCapability],
) -> dict[str, object]:
    """Build the flat, fully-required provider boundary schema."""

    capability_ids = tuple(dict.fromkeys(item.capability_id for item in capabilities[:50]))
    properties: dict[str, object] = {
        "dialogue_act": {"type": "string", "enum": [item.value for item in AssistantDialogueAct]},
        "domain": {"type": "string", "enum": [item.value for item in AssistantDomain]},
        "capability_id": _nullable_string(max_length=100, enum=capability_ids),
        "target_entity_id": _nullable_string(max_length=300),
        "confidence_scaled": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": _nullable_string(max_length=500),
        "inferred_action": {"type": "string", "enum": [item.value for item in PlannerInferredAction]},
        "argument_items": {
            "type": "array",
            "maxItems": MAX_ARGUMENT_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "value": {"type": "string", "minLength": 1, "maxLength": MAX_ARGUMENT_VALUE_LENGTH},
                },
                "required": ["name", "value"],
            },
        },
        "referenced_candidate_ids": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "provider_notes_code": _nullable_string(
            max_length=40, enum=[item.value for item in PlannerNotesCode]
        ),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def groq_planner_wire_schema(
    capabilities: Sequence[PlannerCapability],
) -> dict[str, object]:
    return planner_wire_schema(capabilities)


def gemini_planner_wire_schema(
    capabilities: Sequence[PlannerCapability],
) -> dict[str, object]:
    # Explicit boundary even while fields align; either provider can evolve
    # without leaking its wire format into the canonical contract.
    return planner_wire_schema(capabilities)


def _safe_request_data(
    snapshot: PlannerContextSnapshot,
    message: str,
    capabilities: Sequence[PlannerCapability],
) -> dict[str, object]:
    return {
        "request": message[:2_000],
        "context": snapshot.model_dump(mode="json"),
        "capabilities": [item.model_dump(mode="json") for item in capabilities[:50]],
    }


def _known_entity_ids(snapshot: PlannerContextSnapshot) -> frozenset[str]:
    return frozenset(
        value
        for value in (snapshot.last_successful_entity_id, *snapshot.candidate_entity_ids)
        if value
    )


def _capability_domain(contract: PlannerCapability) -> AssistantDomain:
    return {
        "catalog": AssistantDomain.CATALOG,
        "warehouse": AssistantDomain.WAREHOUSE,
        "attendance": AssistantDomain.ATTENDANCE,
        "employees": AssistantDomain.EMPLOYEES,
        "reports": AssistantDomain.REPORTS,
        "administration": AssistantDomain.ADMINISTRATION,
        "recipients": AssistantDomain.ADMINISTRATION,
        "reminders": AssistantDomain.ADMINISTRATION,
        "schedules": AssistantDomain.ADMINISTRATION,
        "help": AssistantDomain.GENERAL,
        "navigation": AssistantDomain.GENERAL,
        "general": AssistantDomain.GENERAL,
    }.get(contract.domain, AssistantDomain.UNSPECIFIED)


def _convert_envelope(
    envelope: GroqPlannerEnvelope | GeminiPlannerEnvelope,
    *,
    snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
) -> PlannerDecision:
    """Convert untrusted wire data into the authoritative BarBot contract."""

    contracts = {item.capability_id: item for item in capabilities[:50]}
    contract = contracts.get(envelope.capability_id) if envelope.capability_id else None
    if envelope.capability_id is not None and contract is None:
        raise ValueError("planner selected an unavailable capability")
    if (
        envelope.capability_id is not None
        and envelope.capability_id not in snapshot.available_capability_ids
    ):
        raise ValueError("planner capability escaped the snapshot allowlist")
    if contract is not None and envelope.domain is not _capability_domain(contract):
        raise ValueError("planner domain disagrees with capability")
    expected_actions = {
        "catalog.search": PlannerInferredAction.SEARCH,
        "catalog.get": PlannerInferredAction.GET,
        "catalog.manage": PlannerInferredAction.MANAGE,
        "warehouse.prepare_writeoff": PlannerInferredAction.WRITEOFF,
        "warehouse.prepare_transfer": PlannerInferredAction.TRANSFER,
        "warehouse.cancel_batch": PlannerInferredAction.CANCEL,
        "warehouse.commit_batch": PlannerInferredAction.CONFIRM,
        "reports.generate": PlannerInferredAction.GENERATE,
        "reports.send": PlannerInferredAction.SEND,
    }
    if contract is None:
        if envelope.inferred_action is not PlannerInferredAction.UNSPECIFIED:
            raise ValueError("planner supplied action without capability")
    elif (
        contract.capability_id in expected_actions
        and envelope.inferred_action is not expected_actions[contract.capability_id]
    ):
        raise ValueError("planner action disagrees with capability")
    known_ids = _known_entity_ids(snapshot)
    if envelope.target_entity_id and envelope.target_entity_id not in known_ids:
        raise ValueError("planner invented an entity ID")
    if any(value not in known_ids for value in envelope.referenced_candidate_ids):
        raise ValueError("planner referenced an unavailable candidate ID")

    arguments: dict[str, str] = {}
    allowed_arguments = frozenset(contract.allowed_arguments if contract else ())
    for item in envelope.argument_items:
        if item.name in arguments:
            raise ValueError("planner repeated an argument")
        if contract is None or item.name not in allowed_arguments:
            raise ValueError("planner returned an unknown argument")
        arguments[item.name] = item.value
    # Entity references are derived from the separately validated stable ID,
    # never copied from a free-form provider argument.
    if contract is not None and envelope.target_entity_id:
        if contract.capability_id in {"catalog.get", "attendance.get", "reports.status"}:
            arguments["entity_ref"] = envelope.target_entity_id
        elif contract.capability_id in {
            "catalog.manage",
            "users.change_role",
            "attendance.force_close",
            "warehouse.update_operation",
            "warehouse.delete_operation",
            "warehouse.close_shift",
            "warehouse.open_correction",
            "warehouse.commit_correction",
            "reports.send",
            "recipients.create_or_update",
            "recipients.disable",
            "reminders.update",
            "schedules.create_or_update",
            "schedules.disable",
        }:
            arguments["target_ref"] = envelope.target_entity_id
    if contract is not None and not envelope.needs_clarification:
        missing = set(contract.required_arguments) - set(arguments)
        if missing:
            raise ValueError("planner omitted a required argument")
    if envelope.needs_clarification != bool(envelope.clarification_question):
        raise ValueError("planner clarification fields disagree")

    read_only = bool(contract and contract.read_only)
    mutation = bool(contract and contract.mutation)
    if contract is None:
        # A no-capability decision is informational/clarifying, never mutation.
        read_only, mutation = True, False
    return PlannerDecision(
        dialogue_act=envelope.dialogue_act,
        domain=envelope.domain,
        capability_id=envelope.capability_id,
        target_entity_id=envelope.target_entity_id,
        arguments=arguments,
        confidence=envelope.confidence_scaled,
        needs_clarification=envelope.needs_clarification,
        clarification_question=envelope.clarification_question,
        read_only=read_only,
        mutation=mutation,
        # Provider-proposed context updates are deliberately absent. The
        # orchestrator/tool result owns persistent context changes.
        context_updates=PlannerContextUpdates(),
    )


def convert_groq_envelope(
    envelope: GroqPlannerEnvelope,
    *, snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
) -> PlannerDecision:
    return _convert_envelope(envelope, snapshot=snapshot, capabilities=capabilities)


def convert_gemini_envelope(
    envelope: GeminiPlannerEnvelope,
    *, snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
) -> PlannerDecision:
    return _convert_envelope(envelope, snapshot=snapshot, capabilities=capabilities)


def _normalized_error(kind: ProviderErrorKind) -> ProviderPlanError:
    return {
        ProviderErrorKind.AUTH: ProviderPlanError.AUTH,
        ProviderErrorKind.BILLING: ProviderPlanError.QUOTA,
        ProviderErrorKind.CONFIG: ProviderPlanError.CONFIG,
        ProviderErrorKind.RATE_LIMIT: ProviderPlanError.RATE_LIMIT,
        ProviderErrorKind.SERVER: ProviderPlanError.SERVER_ERROR,
        ProviderErrorKind.TIMEOUT: ProviderPlanError.TIMEOUT,
        ProviderErrorKind.UNAVAILABLE: ProviderPlanError.NETWORK,
        ProviderErrorKind.MALFORMED_RESPONSE: ProviderPlanError.MALFORMED_RESPONSE,
        ProviderErrorKind.RESPONSE_TOO_LARGE: ProviderPlanError.MALFORMED_RESPONSE,
        ProviderErrorKind.SCHEMA_INCOMPATIBLE: ProviderPlanError.INVALID_SCHEMA,
    }[kind]


def _canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


_SAFE_ERROR_TYPES = {
    item.value: item
    for item in SafeProviderErrorType
    if item is not SafeProviderErrorType.UNKNOWN
}
_SAFE_ERROR_CODES = {
    item.value: item
    for item in SafeProviderErrorCode
    if item is not SafeProviderErrorCode.UNKNOWN
}
_SAFE_ERROR_PARAMS = frozenset(
    {
        "response_format",
        "json_schema",
        "messages",
        "model",
        "reasoning_effort",
        "max_completion_tokens",
    }
)


def _provider_error_diagnostic(
    *,
    status: int | None,
    body: bytes,
    request_hash: str,
    schema_hash: str,
    transport_error: ProviderPlanError | None = None,
) -> ProviderErrorDiagnostic:
    """Parse only allowlisted Groq metadata and discard all provider prose."""

    raw_type: object = None
    raw_code: object = None
    raw_param: object = None
    if body:
        try:
            decoded = json.loads(body)
            error = decoded.get("error") if isinstance(decoded, dict) else None
            if isinstance(error, dict):
                raw_type = error.get("type")
                raw_code = error.get("code")
                raw_param = error.get("param")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    safe_type = (
        _SAFE_ERROR_TYPES.get(raw_type, SafeProviderErrorType.UNKNOWN)
        if isinstance(raw_type, str)
        else SafeProviderErrorType.UNKNOWN
    )
    safe_code = (
        _SAFE_ERROR_CODES.get(raw_code, SafeProviderErrorCode.UNKNOWN)
        if isinstance(raw_code, str)
        else SafeProviderErrorCode.UNKNOWN
    )
    safe_param = (
        raw_param
        if isinstance(raw_param, str) and raw_param in _SAFE_ERROR_PARAMS
        else None
    )

    if transport_error is not None:
        category = transport_error
    elif status in {401, 403} or safe_code is SafeProviderErrorCode.INVALID_API_KEY:
        category = ProviderPlanError.AUTH
    elif status == 402 or safe_code in {
        SafeProviderErrorCode.INSUFFICIENT_QUOTA,
        SafeProviderErrorCode.QUOTA_EXCEEDED,
    }:
        category = ProviderPlanError.QUOTA
    elif status == 429 or safe_code is SafeProviderErrorCode.RATE_LIMIT_EXCEEDED:
        category = ProviderPlanError.RATE_LIMIT
    elif status is not None and status >= 500:
        category = ProviderPlanError.SERVER_ERROR
    elif safe_code in {
        SafeProviderErrorCode.INVALID_SCHEMA,
        SafeProviderErrorCode.JSON_SCHEMA_INVALID,
    }:
        category = ProviderPlanError.INVALID_SCHEMA
    elif safe_code in {
        SafeProviderErrorCode.JSON_VALIDATE_FAILED,
        SafeProviderErrorCode.GENERATED_OUTPUT_SCHEMA_MISMATCH,
    }:
        category = ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH
    elif safe_code is SafeProviderErrorCode.UNSUPPORTED_PARAMETER:
        category = ProviderPlanError.UNSUPPORTED_PARAMETER
    elif (
        safe_code is SafeProviderErrorCode.MODEL_REFUSAL
        or safe_type is SafeProviderErrorType.MODEL_REFUSAL
    ):
        category = ProviderPlanError.MODEL_REFUSAL
    elif (
        safe_code is SafeProviderErrorCode.SAFETY_REFUSAL
        or safe_type is SafeProviderErrorType.SAFETY_REFUSAL
    ):
        category = ProviderPlanError.SAFETY_REFUSAL
    elif status == 400:
        category = ProviderPlanError.UNKNOWN_BAD_REQUEST
    else:
        category = ProviderPlanError.CONFIG
    retryable = category in {
        ProviderPlanError.RATE_LIMIT,
        ProviderPlanError.SERVER_ERROR,
        ProviderPlanError.TIMEOUT,
        ProviderPlanError.NETWORK,
    }
    return ProviderErrorDiagnostic(
        http_status=status,
        provider_error_type=safe_type,
        provider_error_code=safe_code,
        parameter=safe_param,
        category=category,
        retryable=retryable,
        response_bytes=len(body),
        request_hash=request_hash,
        schema_hash=schema_hash,
    )


class _PlannerProviderBase:
    envelope_type: type[GroqPlannerEnvelope] | type[GeminiPlannerEnvelope]
    schema_name: str
    schema_mode: str

    def __init__(
        self,
        transport: AssistantHttpTransport,
        *,
        api_key: str,
        model: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        total_timeout_seconds: float,
    ) -> None:
        self.transport = transport
        self._api_key = api_key
        self.model = model
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds

    def _result(
        self,
        started: float,
        *,
        decision: PlannerDecision | None = None,
        error: ProviderPlanError | None = None,
        diagnostic: ProviderErrorDiagnostic | None = None,
        status: PlannerValidationStatus,
    ) -> ProviderPlanResult:
        return ProviderPlanResult(
            provider=self.provider_name,
            model=self.model,
            decision=decision,
            error=error,
            diagnostic=diagnostic,
            latency_ms=max(0, round((monotonic() - started) * 1_000)),
            schema_mode=self.schema_mode,
            validation_status=status,
        )

    async def _post(self, url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> HttpResponse:
        return await self.transport.post_json(
            url,
            headers=headers,
            payload=payload,
            timeout_seconds=self.total_timeout_seconds,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )

    async def _safe_post(self, url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> HttpResponse:
        try:
            response = await self._post(url, headers, payload)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError(self.provider_name, ProviderErrorKind.TIMEOUT) from exc
        except _ResponseTooLargeError as exc:
            raise ProviderError(
                self.provider_name, ProviderErrorKind.RESPONSE_TOO_LARGE
            ) from exc
        except aiohttp.ClientError as exc:
            # Transport exceptions are sanitized here; raw URL/header/provider
            # bodies never enter logs or result objects.
            raise ProviderError(self.provider_name, ProviderErrorKind.UNAVAILABLE) from exc
        kind = self._status_kind(response.status)
        if kind is not None:
            raise ProviderError(self.provider_name, kind)
        return response

    @staticmethod
    def _status_kind(status: int) -> ProviderErrorKind | None:
        if status in {401, 403}:
            return ProviderErrorKind.AUTH
        if status == 402:
            return ProviderErrorKind.BILLING
        if status == 429:
            return ProviderErrorKind.RATE_LIMIT
        if status == 400:
            return ProviderErrorKind.SCHEMA_INCOMPATIBLE
        if status in {404, 405, 422}:
            return ProviderErrorKind.CONFIG
        if status >= 500:
            return ProviderErrorKind.SERVER
        if status >= 400:
            return ProviderErrorKind.UNAVAILABLE
        return None


class GroqPlannerProvider(_PlannerProviderBase):
    provider_name = "groq"
    envelope_type = GroqPlannerEnvelope
    schema_name = GROQ_PLANNER_SCHEMA_NAME
    schema_mode = "json_schema_strict"

    def __init__(self, transport: AssistantHttpTransport, *, api_key: str, model: str = "openai/gpt-oss-20b", connect_timeout_seconds: float = 3, read_timeout_seconds: float = 5, total_timeout_seconds: float = 8) -> None:
        super().__init__(transport, api_key=api_key, model=model, connect_timeout_seconds=connect_timeout_seconds, read_timeout_seconds=read_timeout_seconds, total_timeout_seconds=total_timeout_seconds)

    async def plan(self, snapshot: PlannerContextSnapshot, message: str, allowed_capabilities: Sequence[PlannerCapability]) -> ProviderPlanResult:
        started = monotonic()
        capabilities = tuple(allowed_capabilities[:50])
        schema = groq_planner_wire_schema(capabilities)
        payload: Mapping[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_assistant_system_prompt()},
                {"role": "user", "content": json.dumps(_safe_request_data(snapshot, message, capabilities), ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": 0,
            "max_completion_tokens": 800,
            "reasoning_effort": "low",
            "response_format": {"type": "json_schema", "json_schema": {"name": self.schema_name, "strict": True, "schema": schema}},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PLANNER_REQUEST_BYTES:
            return self._result(started, error=ProviderPlanError.CONFIG, status=PlannerValidationStatus.POLICY_REJECTED)
        request_hash = _canonical_hash(payload)
        schema_hash = _canonical_hash(schema)
        try:
            response = await self._post(
                GROQ_CHAT_COMPLETIONS_URL,
                {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                payload,
            )
            if response.status >= 400:
                diagnostic = _provider_error_diagnostic(
                    status=response.status,
                    body=response.body,
                    request_hash=request_hash,
                    schema_hash=schema_hash,
                )
                return self._result(
                    started,
                    error=diagnostic.category,
                    diagnostic=diagnostic,
                    status=PlannerValidationStatus.PROVIDER_ERROR,
                )
            outer = json.loads(response.body)
            raw = outer["choices"][0]["message"]["content"]
            envelope = GroqPlannerEnvelope.model_validate_json(raw)
            decision = convert_groq_envelope(envelope, snapshot=snapshot, capabilities=capabilities)
            return self._result(started, decision=decision, status=PlannerValidationStatus.PASSED)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            diagnostic = _provider_error_diagnostic(
                status=None,
                body=b"",
                request_hash=request_hash,
                schema_hash=schema_hash,
                transport_error=ProviderPlanError.TIMEOUT,
            )
            return self._result(
                started,
                error=diagnostic.category,
                diagnostic=diagnostic,
                status=PlannerValidationStatus.PROVIDER_ERROR,
            )
        except _ResponseTooLargeError:
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.WIRE_INVALID)
        except aiohttp.ClientError:
            diagnostic = _provider_error_diagnostic(
                status=None,
                body=b"",
                request_hash=request_hash,
                schema_hash=schema_hash,
                transport_error=ProviderPlanError.NETWORK,
            )
            return self._result(
                started,
                error=diagnostic.category,
                diagnostic=diagnostic,
                status=PlannerValidationStatus.PROVIDER_ERROR,
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError):
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.WIRE_INVALID)
        except ValueError:
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.CANONICAL_INVALID)
        except Exception:
            return self._result(started, error=ProviderPlanError.INTERNAL, status=PlannerValidationStatus.PROVIDER_ERROR)


class GeminiPlannerProvider(_PlannerProviderBase):
    provider_name = "gemini"
    envelope_type = GeminiPlannerEnvelope
    schema_name = GEMINI_PLANNER_SCHEMA_NAME
    schema_mode = "response_json_schema"

    def __init__(self, transport: AssistantHttpTransport, *, api_key: str, model: str, connect_timeout_seconds: float = 3, read_timeout_seconds: float = 8, total_timeout_seconds: float = 10) -> None:
        super().__init__(transport, api_key=api_key, model=model, connect_timeout_seconds=connect_timeout_seconds, read_timeout_seconds=read_timeout_seconds, total_timeout_seconds=total_timeout_seconds)

    async def plan(self, snapshot: PlannerContextSnapshot, message: str, allowed_capabilities: Sequence[PlannerCapability]) -> ProviderPlanResult:
        started = monotonic()
        capabilities = tuple(allowed_capabilities[:50])
        schema = gemini_planner_wire_schema(capabilities)
        payload: Mapping[str, object] = {
            "system_instruction": {"parts": [{"text": build_assistant_system_prompt()}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(_safe_request_data(snapshot, message, capabilities), ensure_ascii=False, separators=(",", ":"))}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseJsonSchema": schema, "maxOutputTokens": 800},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PLANNER_REQUEST_BYTES:
            return self._result(started, error=ProviderPlanError.CONFIG, status=PlannerValidationStatus.POLICY_REJECTED)
        try:
            response = await self._safe_post(
                f"{GEMINI_API_ROOT}/{self.model}:generateContent",
                {"x-goog-api-key": self._api_key},
                payload,
            )
            outer = json.loads(response.body)
            raw = outer["candidates"][0]["content"]["parts"][0]["text"]
            envelope = GeminiPlannerEnvelope.model_validate_json(raw)
            decision = convert_gemini_envelope(envelope, snapshot=snapshot, capabilities=capabilities)
            return self._result(started, decision=decision, status=PlannerValidationStatus.PASSED)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            return self._result(started, error=_normalized_error(exc.kind), status=PlannerValidationStatus.PROVIDER_ERROR)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError):
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.WIRE_INVALID)
        except ValueError:
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.CANONICAL_INVALID)
        except Exception:
            return self._result(started, error=ProviderPlanError.INTERNAL, status=PlannerValidationStatus.PROVIDER_ERROR)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _CircuitRecord:
    failures: int = 0
    opened_at: float | None = None
    probe_active: bool = False


class PlannerCircuitBreaker:
    """Small process-local provider health tracker; owns no business state."""

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30, clock=monotonic) -> None:
        if failure_threshold < 1 or cooldown_seconds <= 0:
            raise ValueError("invalid circuit breaker bounds")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._records: dict[str, _CircuitRecord] = {}

    def state(self, provider: str) -> CircuitState:
        record = self._records.setdefault(provider, _CircuitRecord())
        if record.opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - record.opened_at >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def acquire(self, provider: str) -> bool:
        state = self.state(provider)
        record = self._records[provider]
        if state is CircuitState.OPEN or (state is CircuitState.HALF_OPEN and record.probe_active):
            return False
        if state is CircuitState.HALF_OPEN:
            record.probe_active = True
        return True

    def success(self, provider: str) -> None:
        self._records[provider] = _CircuitRecord()

    def failure(self, provider: str, *, transient: bool) -> None:
        record = self._records.setdefault(provider, _CircuitRecord())
        record.probe_active = False
        if not transient:
            return
        record.failures += 1
        if record.failures >= self.failure_threshold:
            record.opened_at = self._clock()


_FALLBACK_ERRORS = frozenset({
    ProviderPlanError.TIMEOUT,
    ProviderPlanError.NETWORK,
    ProviderPlanError.RATE_LIMIT,
    ProviderPlanError.SERVER_ERROR,
    ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH,
    ProviderPlanError.MODEL_REFUSAL,
    ProviderPlanError.MALFORMED_RESPONSE,
    ProviderPlanError.CIRCUIT_OPEN,
})

_CIRCUIT_TRANSIENT_ERRORS = frozenset({
    ProviderPlanError.TIMEOUT,
    ProviderPlanError.NETWORK,
    ProviderPlanError.RATE_LIMIT,
    ProviderPlanError.SERVER_ERROR,
})


class MultiProviderPlanner:
    """Expose canonical decisions while keeping provider wire formats private."""

    def __init__(self, primary: PlannerProvider | None, fallback: PlannerProvider | None = None, *, mode: str = "local_first", total_deadline_seconds: float = 12, shadow_fallback_enabled: bool = False, circuit_breaker: PlannerCircuitBreaker | None = None) -> None:
        if mode not in {"local_first", "shadow", "ai_first"}:
            raise ValueError("unknown planner mode")
        self.primary = primary
        self.fallback = fallback
        self.mode = mode
        self.total_deadline_seconds = total_deadline_seconds
        self.shadow_fallback_enabled = shadow_fallback_enabled
        self.circuit = circuit_breaker or PlannerCircuitBreaker()

    async def _call(self, provider: PlannerProvider, snapshot: PlannerContextSnapshot, text: str, capabilities: Sequence[PlannerCapability]) -> ProviderPlanResult:
        if not self.circuit.acquire(provider.provider_name):
            return ProviderPlanResult(provider=provider.provider_name, model=provider.model, error=ProviderPlanError.CIRCUIT_OPEN, latency_ms=0, schema_mode="skipped", validation_status=PlannerValidationStatus.PROVIDER_ERROR)
        result = await provider.plan(snapshot, text, capabilities)
        transient = result.error in _CIRCUIT_TRANSIENT_ERRORS
        if result.decision is not None:
            self.circuit.success(provider.provider_name)
        else:
            self.circuit.failure(provider.provider_name, transient=transient)
        return result

    async def plan(self, text: str, *, snapshot: PlannerContextSnapshot, capabilities: Sequence[PlannerCapability]) -> PlannerDecision:
        providers = [provider for provider in (self.primary, self.fallback) if provider is not None]
        if not providers:
            raise ProviderError("planner", ProviderErrorKind.UNAVAILABLE)
        if self.mode == "shadow" and not self.shadow_fallback_enabled:
            providers = providers[:1]
        started = monotonic()
        last: ProviderPlanResult | None = None
        for index, provider in enumerate(providers):
            remaining = self.total_deadline_seconds - (monotonic() - started)
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(remaining):
                    last = await self._call(provider, snapshot, text, capabilities)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                last = ProviderPlanResult(provider=provider.provider_name, model=provider.model, error=ProviderPlanError.DEADLINE, latency_ms=max(0, round((monotonic() - started) * 1_000)), schema_mode="deadline", validation_status=PlannerValidationStatus.PROVIDER_ERROR)
            if last.decision is not None:
                return last.decision
            if last.error not in _FALLBACK_ERRORS or index + 1 >= len(providers):
                break
        kind = ProviderErrorKind.TIMEOUT if last and last.error in {ProviderPlanError.TIMEOUT, ProviderPlanError.DEADLINE} else ProviderErrorKind.UNAVAILABLE
        raise ProviderError(last.provider if last else "planner", kind)
