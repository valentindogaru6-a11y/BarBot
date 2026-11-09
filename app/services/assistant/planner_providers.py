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
    PlannerTargetOption,
)
from app.models.assistant_provider_planner import (
    GeminiPlannerRouteEnvelope,
    GroqPlannerRouteEnvelope,
    PlannerRouteActionCode,
    PlannerRouteDomain,
    PlannerRouteEnvelope,
    PlannerValidationStatus,
    ProviderErrorDiagnostic,
    ProviderPlanError,
    ProviderPlanResult,
    PlannerAuthorityError,
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
from app.services.assistant.planner_arguments import local_stage_two_arguments
from app.services.assistant.system_prompt import build_planner_route_system_prompt
from app.services.assistant.planner_policy import (
    ATTEMPT_ROLE, CURRENT_REQUEST, GovernedPlannerTransport, PlannerBusy, planner_request,
)

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_PLANNER_SCHEMA_NAME = "barbot_groq_route_v2"
GEMINI_PLANNER_SCHEMA_NAME = "barbot_gemini_route_v2"


class PlannerProvider(Protocol):
    provider_name: str
    model: str

    async def plan(
        self,
        snapshot: PlannerContextSnapshot,
        message: str,
        allowed_capabilities: Sequence[PlannerCapability],
    ) -> ProviderPlanResult: ...


def planner_route_schema(
    capabilities: Sequence[PlannerCapability],
    *,
    target_count: int = 0,
) -> dict[str, object]:
    """Return a small Stage 1 schema with no IDs, arguments or authority."""

    capability_ids = tuple(
        dict.fromkeys(item.capability_id for item in capabilities[:50])
    )
    properties: dict[str, object] = {
        "dialogue_act": {
            "type": "string",
            "enum": [item.value for item in AssistantDialogueAct],
        },
        "domain": {
            "type": "string",
            "enum": [item.value for item in PlannerRouteDomain],
        },
        "capability_id": {
            "type": "string",
            "enum": [*capability_ids, "none"],
        },
        "target_index": {
            "type": "integer",
            "minimum": -1,
            "maximum": max(-1, min(target_count, 5) - 1),
        },
        "action_code": {
            "type": "string",
            "enum": [item.value for item in PlannerRouteActionCode],
        },
        "needs_clarification": {"type": "boolean"},
        "confidence_scaled": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def groq_planner_route_schema(
    capabilities: Sequence[PlannerCapability], *, target_count: int = 0
) -> dict[str, object]:
    return planner_route_schema(capabilities, target_count=target_count)


def gemini_planner_route_schema(
    capabilities: Sequence[PlannerCapability], *, target_count: int = 0
) -> dict[str, object]:
    return planner_route_schema(capabilities, target_count=target_count)


def _route_domain_for_capability(contract: PlannerCapability) -> PlannerRouteDomain:
    prefix = contract.capability_id.split(".", 1)[0]
    if prefix == "attendance_reports":
        return PlannerRouteDomain.REPORTS
    if prefix == "capabilities":
        return PlannerRouteDomain.GENERAL
    if prefix in {item.value for item in PlannerRouteDomain}:
        return PlannerRouteDomain(prefix)
    return {
        "employees": PlannerRouteDomain.USERS,
        "help": PlannerRouteDomain.GENERAL,
        "administration": PlannerRouteDomain.GENERAL,
    }.get(contract.domain, PlannerRouteDomain.GENERAL)


def _canonical_domain(domain: PlannerRouteDomain) -> AssistantDomain:
    return {
        PlannerRouteDomain.CATALOG: AssistantDomain.CATALOG,
        PlannerRouteDomain.WAREHOUSE: AssistantDomain.WAREHOUSE,
        PlannerRouteDomain.ATTENDANCE: AssistantDomain.ATTENDANCE,
        PlannerRouteDomain.REPORTS: AssistantDomain.REPORTS,
        PlannerRouteDomain.USERS: AssistantDomain.EMPLOYEES,
        PlannerRouteDomain.RECIPIENTS: AssistantDomain.ADMINISTRATION,
        PlannerRouteDomain.REMINDERS: AssistantDomain.ADMINISTRATION,
        PlannerRouteDomain.SCHEDULES: AssistantDomain.ADMINISTRATION,
        PlannerRouteDomain.NAVIGATION: AssistantDomain.GENERAL,
        PlannerRouteDomain.GENERAL: AssistantDomain.GENERAL,
    }[domain]


def planner_target_options(
    snapshot: PlannerContextSnapshot,
) -> tuple[PlannerTargetOption, ...]:
    """Build a server index map while retaining old snapshot compatibility."""

    if snapshot.target_options:
        return tuple(snapshot.target_options[:5])
    options: list[PlannerTargetOption] = []
    current_domain = snapshot.current_domain.value
    safe_domain = current_domain if current_domain != "unspecified" else "general"
    if snapshot.last_successful_entity_id and snapshot.last_successful_entity_name:
        options.append(PlannerTargetOption(
            stable_id=snapshot.last_successful_entity_id,
            display_name=snapshot.last_successful_entity_name,
            entity_kind="selected_entity",
            relation_label="last_successful",
            domain=safe_domain,
        ))
    known = {item.stable_id for item in options}
    for index, stable_id in enumerate(snapshot.candidate_entity_ids):
        if stable_id in known:
            continue
        options.append(PlannerTargetOption(
            stable_id=stable_id,
            display_name=f"Вариант {index + 1}",
            entity_kind="candidate",
            relation_label="candidate",
            domain=safe_domain,
        ))
        if len(options) == 5:
            break
    return tuple(options)


def _safe_route_request_data(
    snapshot: PlannerContextSnapshot,
    message: str,
    capabilities: Sequence[PlannerCapability],
) -> dict[str, object]:
    targets = planner_target_options(snapshot)
    return {
        "request": message[:2_000],
        "context": {
            "actor_role": snapshot.actor_role,
            "current_domain": snapshot.current_domain.value,
            "pending_clarification": snapshot.pending_clarification or "",
            "pending_action": snapshot.pending_action or "",
            "last_tool_operation": snapshot.last_tool_operation or "",
            "last_tool_summary": snapshot.last_tool_summary or "",
            "recent_turns": [
                item.model_dump(mode="json") for item in snapshot.recent_turns
            ],
            "owner_memories": [item.model_dump(mode="json") for item in snapshot.owner_memories],
            "tool_results": [item.model_dump(mode="json") for item in snapshot.tool_results],
            "targets": [
                {
                    "index": index,
                    "display_name": item.display_name,
                    "entity_kind": item.entity_kind,
                    "relation_label": item.relation_label,
                    "domain": item.domain,
                }
                for index, item in enumerate(targets)
            ],
        },
        "capabilities": [
            {
                "capability_id": item.capability_id,
                "domain": _route_domain_for_capability(item).value,
            }
            for item in capabilities[:50]
        ],
    }


def assemble_route_decision(
    envelope: PlannerRouteEnvelope,
    *,
    snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
    arguments: Mapping[str, object] | None = None,
) -> PlannerDecision:
    """Resolve indices and derive risk, target and clarification locally."""

    contracts = {item.capability_id: item for item in capabilities[:50]}
    capability_id = None if envelope.capability_id == "none" else envelope.capability_id
    contract = contracts.get(capability_id) if capability_id else None
    if capability_id is not None and contract is None:
        raise PlannerAuthorityError("planner selected an unavailable capability")
    if capability_id is not None and capability_id not in snapshot.available_capability_ids:
        raise PlannerAuthorityError("planner capability escaped the snapshot allowlist")
    if contract is not None and envelope.domain is not _route_domain_for_capability(contract):
        raise PlannerAuthorityError("planner domain disagrees with capability")
    expected_actions = {
        "catalog.search": {PlannerRouteActionCode.SEARCH},
        "catalog.get": {PlannerRouteActionCode.OPEN},
        "catalog.manage": {
            PlannerRouteActionCode.EDIT,
            PlannerRouteActionCode.ADD,
            PlannerRouteActionCode.REMOVE,
            PlannerRouteActionCode.REPLACE,
        },
        "warehouse.prepare_writeoff": {PlannerRouteActionCode.CREATE},
        "warehouse.prepare_transfer": {PlannerRouteActionCode.CREATE},
        "warehouse.cancel_batch": {PlannerRouteActionCode.CANCEL},
        "warehouse.commit_batch": {PlannerRouteActionCode.CONFIRM},
        "reports.generate": {PlannerRouteActionCode.GENERATE},
        "reports.send": {PlannerRouteActionCode.SEND},
    }
    if (
        contract is not None
        and contract.capability_id in expected_actions
        and envelope.action_code not in expected_actions[contract.capability_id]
    ):
        raise ValueError("planner action disagrees with capability")

    targets = planner_target_options(snapshot)
    target: PlannerTargetOption | None = None
    if envelope.target_index >= 0:
        if envelope.target_index >= len(targets):
            raise PlannerAuthorityError("planner target index is out of range")
        target = targets[envelope.target_index]
        if target.domain != envelope.domain.value:
            raise PlannerAuthorityError("planner target belongs to another domain")
    target_required = bool(
        contract
        and (
            {"entity_ref", "target_ref"} & set(contract.required_arguments)
            or (
                contract.capability_id == "catalog.manage"
                and envelope.action_code in {
                    PlannerRouteActionCode.EDIT,
                    PlannerRouteActionCode.REMOVE,
                    PlannerRouteActionCode.REPLACE,
                }
            )
        )
    )
    if target_required and target is None:
        raise PlannerAuthorityError("planner omitted a required target")
    if contract is None and target is not None:
        raise PlannerAuthorityError("planner supplied target without capability")

    resolved_arguments = dict(arguments or {})
    if contract is not None:
        unknown = set(resolved_arguments) - set(contract.allowed_arguments)
        if unknown:
            raise ValueError("local extractor returned an unknown argument")
    if contract is not None and target is not None:
        if contract.capability_id in {"catalog.get", "attendance.get", "reports.status"}:
            resolved_arguments["entity_ref"] = target.stable_id
        elif contract.capability_id in {
            "catalog.manage", "users.change_role", "attendance.force_close",
            "warehouse.update_operation", "warehouse.delete_operation",
            "warehouse.close_shift", "warehouse.open_correction",
            "warehouse.commit_correction", "reports.send",
            "recipients.create_or_update", "recipients.disable",
            "reminders.update", "schedules.create_or_update",
            "schedules.disable",
        }:
            resolved_arguments["target_ref"] = target.stable_id

    missing = (
        set(contract.required_arguments) - set(resolved_arguments)
        if contract is not None
        else set()
    )
    if contract is not None:
        stage_two_required = {
            "warehouse.prepare_writeoff": {"product", "quantity", "unit"},
            "warehouse.prepare_transfer": {"product", "quantity", "unit", "destination"},
            "attendance.force_close": {"target_ref", "reason"},
        }.get(contract.capability_id, set())
        missing.update(
            name for name in stage_two_required if not resolved_arguments.get(name)
        )
    needs_clarification = envelope.needs_clarification or bool(missing)
    label = target.display_name if target is not None else "выбранной позиции"
    if needs_clarification:
        if contract is not None and contract.capability_id == "catalog.manage":
            clarification = f"Что именно изменить в составе «{label}»?"
        elif "query" in missing:
            clarification = "Уточните, что именно нужно найти."
        elif "destination" in missing:
            clarification = "Уточните направление перемещения."
        elif "quantity" in missing:
            clarification = "Уточните количество."
        elif "unit" in missing:
            clarification = "Уточните единицу измерения."
        elif "product" in missing:
            clarification = "Уточните продукт."
        elif "reason" in missing:
            clarification = "Укажите причину действия."
        else:
            clarification = "Уточните недостающие параметры действия."
    else:
        clarification = None

    read_only = bool(contract.read_only) if contract is not None else True
    mutation = bool(contract.mutation) if contract is not None else False
    if read_only == mutation:
        raise ValueError("runtime capability risk is invalid")
    return PlannerDecision(
        dialogue_act=envelope.dialogue_act,
        domain=_canonical_domain(envelope.domain),
        capability_id=capability_id,
        target_entity_id=target.stable_id if target is not None else None,
        arguments=resolved_arguments,
        confidence=envelope.confidence_scaled,
        needs_clarification=needs_clarification,
        clarification_question=clarification,
        read_only=read_only,
        mutation=mutation,
        context_updates=PlannerContextUpdates(
            current_domain=_canonical_domain(envelope.domain),
            selected_entity_id=target.stable_id if target is not None else None,
            selected_entity_name=target.display_name if target is not None else None,
        ),
    )


def convert_groq_route(
    envelope: GroqPlannerRouteEnvelope,
    *, snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
    arguments: Mapping[str, object] | None = None,
) -> PlannerDecision:
    return assemble_route_decision(
        envelope, snapshot=snapshot, capabilities=capabilities, arguments=arguments
    )


def convert_gemini_route(
    envelope: GeminiPlannerRouteEnvelope,
    *, snapshot: PlannerContextSnapshot,
    capabilities: Sequence[PlannerCapability],
    arguments: Mapping[str, object] | None = None,
) -> PlannerDecision:
    return assemble_route_decision(
        envelope, snapshot=snapshot, capabilities=capabilities, arguments=arguments
    )


def _normalized_error(kind: ProviderErrorKind) -> ProviderPlanError:
    return {
        ProviderErrorKind.SAFETY_REFUSAL: ProviderPlanError.SAFETY_REFUSAL,
        ProviderErrorKind.AUTHORITY: ProviderPlanError.AUTHORITY_VIOLATION,
        ProviderErrorKind.INTERNAL: ProviderPlanError.INTERNAL,
        ProviderErrorKind.BUSY: ProviderPlanError.BUSY,
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
    elif status in {401, 403}:
        category = ProviderPlanError.AUTH
    elif status == 402:
        category = ProviderPlanError.QUOTA
    elif status == 429:
        category = ProviderPlanError.RATE_LIMIT
    elif status is not None and status >= 500:
        category = ProviderPlanError.SERVER_ERROR
    elif status != 400:
        category = (ProviderPlanError.MALFORMED_RESPONSE if status is not None and 200 <= status < 300
                    else ProviderPlanError.CONFIG)
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
    envelope_type: type[GroqPlannerRouteEnvelope] | type[GeminiPlannerRouteEnvelope]
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
    envelope_type = GroqPlannerRouteEnvelope
    schema_name = GROQ_PLANNER_SCHEMA_NAME
    schema_mode = "json_schema_strict"

    def __init__(self, transport: AssistantHttpTransport, *, api_key: str, model: str = "openai/gpt-oss-20b", connect_timeout_seconds: float = 3, read_timeout_seconds: float = 5, total_timeout_seconds: float = 8) -> None:
        super().__init__(transport, api_key=api_key, model=model, connect_timeout_seconds=connect_timeout_seconds, read_timeout_seconds=read_timeout_seconds, total_timeout_seconds=total_timeout_seconds)

    async def plan(self, snapshot: PlannerContextSnapshot, message: str, allowed_capabilities: Sequence[PlannerCapability], *, extract_arguments: bool = True) -> ProviderPlanResult:
        started = monotonic()
        try:
            snapshot = PlannerContextSnapshot.model_validate(snapshot.model_dump())
        except (ValueError, ValidationError):
            return self._result(started, error=ProviderPlanError.CONFIG, status=PlannerValidationStatus.POLICY_REJECTED)
        capabilities = tuple(allowed_capabilities[:50])
        targets = planner_target_options(snapshot)
        schema = groq_planner_route_schema(capabilities, target_count=len(targets))
        payload: Mapping[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_planner_route_system_prompt()},
                {"role": "user", "content": json.dumps(_safe_route_request_data(snapshot, message, capabilities), ensure_ascii=False, separators=(",", ":"))},
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
            if outer['choices'][0]['message'].get('refusal'):
                return self._result(started, error=ProviderPlanError.MODEL_REFUSAL,
                    status=PlannerValidationStatus.PROVIDER_ERROR)
            raw = outer["choices"][0]["message"]["content"]
            envelope = GroqPlannerRouteEnvelope.model_validate_json(raw)
            arguments = (
                local_stage_two_arguments(envelope.capability_id, message)
                if extract_arguments and envelope.capability_id != "none"
                else None
            )
            decision = convert_groq_route(
                envelope,
                snapshot=snapshot,
                capabilities=capabilities,
                arguments=arguments,
            )
            return self._result(started, decision=decision, status=PlannerValidationStatus.PASSED)
        except asyncio.CancelledError:
            raise
        except PlannerBusy:
            return self._result(started, error=ProviderPlanError.BUSY, status=PlannerValidationStatus.PROVIDER_ERROR)
        except ProviderError as exc:
            return self._result(started, error=_normalized_error(exc.kind), status=PlannerValidationStatus.PROVIDER_ERROR)
        except PlannerAuthorityError:
            return self._result(started, error=ProviderPlanError.AUTHORITY_VIOLATION, status=PlannerValidationStatus.CANONICAL_INVALID)
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
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError, ValidationError):
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.WIRE_INVALID)
        except ValueError:
            return self._result(started, error=ProviderPlanError.MALFORMED_RESPONSE, status=PlannerValidationStatus.CANONICAL_INVALID)
        except Exception:
            return self._result(started, error=ProviderPlanError.INTERNAL, status=PlannerValidationStatus.PROVIDER_ERROR)


class GeminiPlannerProvider(_PlannerProviderBase):
    provider_name = "gemini"
    envelope_type = GeminiPlannerRouteEnvelope
    schema_name = GEMINI_PLANNER_SCHEMA_NAME
    schema_mode = "response_json_schema"

    def __init__(self, transport: AssistantHttpTransport, *, api_key: str, model: str, connect_timeout_seconds: float = 3, read_timeout_seconds: float = 8, total_timeout_seconds: float = 10) -> None:
        super().__init__(transport, api_key=api_key, model=model, connect_timeout_seconds=connect_timeout_seconds, read_timeout_seconds=read_timeout_seconds, total_timeout_seconds=total_timeout_seconds)

    async def plan(self, snapshot: PlannerContextSnapshot, message: str, allowed_capabilities: Sequence[PlannerCapability], *, extract_arguments: bool = True) -> ProviderPlanResult:
        started = monotonic()
        try:
            snapshot = PlannerContextSnapshot.model_validate(snapshot.model_dump())
        except (ValueError, ValidationError):
            return self._result(started, error=ProviderPlanError.CONFIG, status=PlannerValidationStatus.POLICY_REJECTED)
        capabilities = tuple(allowed_capabilities[:50])
        targets = planner_target_options(snapshot)
        schema = gemini_planner_route_schema(capabilities, target_count=len(targets))
        payload: Mapping[str, object] = {
            "system_instruction": {"parts": [{"text": build_planner_route_system_prompt()}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(_safe_route_request_data(snapshot, message, capabilities), ensure_ascii=False, separators=(",", ":"))}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseJsonSchema": schema, "maxOutputTokens": 800},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PLANNER_REQUEST_BYTES:
            return self._result(started, error=ProviderPlanError.CONFIG, status=PlannerValidationStatus.POLICY_REJECTED)
        try:
            response = await self._post(
                f"{GEMINI_API_ROOT}/{self.model}:generateContent",
                {"x-goog-api-key": self._api_key},
                payload,
            )
            if response.status >= 400:
                diagnostic = _provider_error_diagnostic(status=response.status, body=response.body,
                    request_hash=_canonical_hash(payload), schema_hash=_canonical_hash(schema))
                return self._result(started, error=diagnostic.category, diagnostic=diagnostic,
                    status=PlannerValidationStatus.PROVIDER_ERROR)
            outer = json.loads(response.body)
            if (outer.get('promptFeedback', {}).get('blockReason') or any(
                candidate.get('finishReason') in {'SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII'}
                for candidate in outer.get('candidates', ())
            )):
                return self._result(started, error=ProviderPlanError.SAFETY_REFUSAL,
                    status=PlannerValidationStatus.PROVIDER_ERROR)
            raw = outer["candidates"][0]["content"]["parts"][0]["text"]
            envelope = GeminiPlannerRouteEnvelope.model_validate_json(raw)
            arguments = (
                local_stage_two_arguments(envelope.capability_id, message)
                if extract_arguments and envelope.capability_id != "none"
                else None
            )
            decision = convert_gemini_route(
                envelope,
                snapshot=snapshot,
                capabilities=capabilities,
                arguments=arguments,
            )
            return self._result(started, decision=decision, status=PlannerValidationStatus.PASSED)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            return self._result(started, error=_normalized_error(exc.kind), status=PlannerValidationStatus.PROVIDER_ERROR)
        except PlannerAuthorityError:
            return self._result(started, error=ProviderPlanError.AUTHORITY_VIOLATION, status=PlannerValidationStatus.CANONICAL_INVALID)
        except (asyncio.TimeoutError, TimeoutError):
            return self._result(started, error=ProviderPlanError.TIMEOUT, status=PlannerValidationStatus.PROVIDER_ERROR)
        except aiohttp.ClientError:
            return self._result(started, error=ProviderPlanError.NETWORK, status=PlannerValidationStatus.PROVIDER_ERROR)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError, ValidationError):
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
    ProviderPlanError.CIRCUIT_OPEN,
    ProviderPlanError.DEADLINE,
})

_CIRCUIT_TRANSIENT_ERRORS = frozenset({
    ProviderPlanError.TIMEOUT,
    ProviderPlanError.NETWORK,
    ProviderPlanError.RATE_LIMIT,
    ProviderPlanError.SERVER_ERROR,
})


class MultiProviderPlanner:
    """Expose canonical decisions while keeping provider wire formats private."""

    def __init__(self, primary: PlannerProvider | None, fallback: PlannerProvider | None = None, *, mode: str = "local_first", total_deadline_seconds: float = 12, shadow_fallback_enabled: bool = False, circuit_breaker: PlannerCircuitBreaker | None = None, attempt_policy=None) -> None:
        if mode not in {"local_first", "shadow", "ai_first"}:
            raise ValueError("unknown planner mode")
        self.primary = primary
        self.fallback = fallback
        self.mode = mode
        self.total_deadline_seconds = total_deadline_seconds
        self.shadow_fallback_enabled = shadow_fallback_enabled
        self.circuit = circuit_breaker or PlannerCircuitBreaker()
        self.attempt_policy = attempt_policy
        if attempt_policy is not None:
            for provider in (primary, fallback):
                if isinstance(provider, _PlannerProviderBase):
                    provider.transport = GovernedPlannerTransport(provider.transport, attempt_policy)

    async def _call(self, provider: PlannerProvider, snapshot: PlannerContextSnapshot, text: str, capabilities: Sequence[PlannerCapability]) -> ProviderPlanResult:
        if not self.circuit.acquire(provider.provider_name):
            return ProviderPlanResult(provider=provider.provider_name, model=provider.model, error=ProviderPlanError.CIRCUIT_OPEN, latency_ms=0, schema_mode="skipped", validation_status=PlannerValidationStatus.PROVIDER_ERROR)
        try:
            if isinstance(provider, _PlannerProviderBase):
                result = await provider.plan(
                    snapshot, text, capabilities, extract_arguments=self.mode != "shadow",
                )
            else:
                result = await provider.plan(snapshot, text, capabilities)
        except BaseException:
            # Release a half-open probe even on cancellation/unexpected errors.
            self.circuit.failure(provider.provider_name, transient=False)
            raise
        if result.decision is not None and result.decision.mutation and self.mode != 'shadow':
            from app.services.assistant.planner_policy import mutation_evidence
            if not mutation_evidence(text):
                result = ProviderPlanResult.model_validate({**result.model_dump(), 'decision': None,
                    'error': ProviderPlanError.AUTHORITY_VIOLATION,
                    'validation_status': PlannerValidationStatus.POLICY_REJECTED})
        transport = getattr(provider, 'transport', None)
        row = None
        if isinstance(transport, GovernedPlannerTransport):
            row, transport.last_row = transport.last_row, None
            if row is not None:
                row['validation'] = result.validation_status.value
                row['category'] = result.error.value if result.error else 'success'
        transient = result.error in _CIRCUIT_TRANSIENT_ERRORS
        if result.decision is not None:
            self.circuit.success(provider.provider_name)
        else:
            self.circuit.failure(provider.provider_name, transient=transient)
        if row is not None:
            row['circuit'] = self.circuit.state(provider.provider_name).value
            transport.policy.emit(row)
        return result

    async def plan(self, text: str, *, snapshot: PlannerContextSnapshot, capabilities: Sequence[PlannerCapability]) -> PlannerDecision:
        with planner_request():
            return await self._plan(text, snapshot=snapshot, capabilities=capabilities)

    def _trace_abort(self, provider, category):
        transport = getattr(provider, 'transport', None)
        if isinstance(transport, GovernedPlannerTransport):
            row, transport.last_row = transport.last_row, None
            if row is not None:
                row['category'] = category
                row['circuit'] = self.circuit.state(provider.provider_name).value
                transport.policy.emit(row)

    async def _plan(self, text: str, *, snapshot: PlannerContextSnapshot, capabilities: Sequence[PlannerCapability]) -> PlannerDecision:
        from app.services.assistant.mutation_language import parse_mutation
        local = parse_mutation(text)
        if local is not None and not local.allowed:
            raise ProviderError('planner', ProviderErrorKind.AUTHORITY)
        providers = [provider for provider in (self.primary, self.fallback) if provider is not None]
        if not providers:
            raise ProviderError("planner", ProviderErrorKind.UNAVAILABLE)
        if self.mode == "shadow" and not self.shadow_fallback_enabled:
            providers = providers[:1]
        started = monotonic()
        state = CURRENT_REQUEST.get()
        assert state is not None
        if state.deadline is None:
            state.deadline = started + self.total_deadline_seconds
        last: ProviderPlanResult | None = None
        for index, provider in enumerate(providers):
            if index and (state.boundary_crossed or snapshot.tool_results or snapshot.pending_action):
                break
            remaining = state.deadline - monotonic()
            if remaining <= 0:
                break
            role_token = ATTEMPT_ROLE.set(('fallback' if index else 'primary', last.error.value if index and last and last.error else None))
            try:
                async with asyncio.timeout(remaining):
                    last = await self._call(provider, snapshot, text, capabilities)
            except asyncio.CancelledError:
                self._trace_abort(provider, 'cancelled')
                raise
            except TimeoutError:
                self.circuit.failure(provider.provider_name, transient=True)
                self._trace_abort(provider, 'deadline')
                last = ProviderPlanResult(provider=provider.provider_name, model=provider.model, error=ProviderPlanError.DEADLINE, latency_ms=max(0, round((monotonic() - started) * 1_000)), schema_mode="deadline", validation_status=PlannerValidationStatus.PROVIDER_ERROR)
            finally:
                ATTEMPT_ROLE.reset(role_token)
            if last.decision is not None:
                return last.decision
            if last.error not in _FALLBACK_ERRORS or index + 1 >= len(providers):
                break
        if last and last.error is ProviderPlanError.BUSY:
            raise PlannerBusy()
        kind = (ProviderErrorKind.TIMEOUT if last and last.error in {ProviderPlanError.TIMEOUT, ProviderPlanError.DEADLINE}
            else ProviderErrorKind.SAFETY_REFUSAL if last and last.error in {ProviderPlanError.SAFETY_REFUSAL, ProviderPlanError.MODEL_REFUSAL}
            else ProviderErrorKind.AUTHORITY if last and last.error is ProviderPlanError.AUTHORITY_VIOLATION
            else ProviderErrorKind.AUTH if last and last.error in {ProviderPlanError.AUTH, ProviderPlanError.QUOTA}
            else ProviderErrorKind.MALFORMED_RESPONSE if last and last.error in {
                ProviderPlanError.MALFORMED_RESPONSE, ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH,
                ProviderPlanError.INVALID_SCHEMA, ProviderPlanError.UNKNOWN_BAD_REQUEST,
            }
            else ProviderErrorKind.CONFIG if last and last.error in {ProviderPlanError.CONFIG, ProviderPlanError.UNSUPPORTED_PARAMETER}
            else ProviderErrorKind.UNAVAILABLE if last and last.error in {
                ProviderPlanError.NETWORK, ProviderPlanError.SERVER_ERROR, ProviderPlanError.RATE_LIMIT, ProviderPlanError.CIRCUIT_OPEN,
            } else ProviderErrorKind.INTERNAL)
        raise ProviderError(last.provider if last else "planner", kind)
