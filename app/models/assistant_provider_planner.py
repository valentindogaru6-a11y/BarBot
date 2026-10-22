"""Provider-boundary planner contracts with no execution authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.assistant import AssistantDomain
from app.models.assistant_planner import AssistantDialogueAct, PlannerDecision


class PlannerInferredAction(StrEnum):
    UNSPECIFIED = "unspecified"
    SEARCH = "search"
    GET = "get"
    MANAGE = "manage"
    WRITEOFF = "writeoff"
    TRANSFER = "transfer"
    LIST = "list"
    GENERATE = "generate"
    SEND = "send"
    CREATE_OR_UPDATE = "create_or_update"
    DISABLE = "disable"
    OPEN = "open"
    CLOSE = "close"
    CANCEL = "cancel"
    CONFIRM = "confirm"


class PlannerNotesCode(StrEnum):
    AMBIGUOUS = "ambiguous"
    MISSING_ARGUMENT = "missing_argument"
    CONTEXT_REFERENCE = "context_reference"
    SAFE_FALLBACK = "safe_fallback"


class PlannerArgumentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1_000)


class _PlannerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dialogue_act: AssistantDialogueAct
    domain: AssistantDomain
    capability_id: str | None = Field(max_length=100)
    target_entity_id: str | None = Field(max_length=300)
    confidence_scaled: int = Field(ge=0, le=10_000)
    needs_clarification: bool
    clarification_question: str | None = Field(max_length=500)
    inferred_action: PlannerInferredAction
    argument_items: tuple[PlannerArgumentItem, ...] = Field(max_length=20)
    referenced_candidate_ids: tuple[str, ...] = Field(max_length=5)
    provider_notes_code: PlannerNotesCode | None


class GroqPlannerEnvelope(_PlannerEnvelope):
    """Flat strict envelope supported by Groq structured output."""


class GeminiPlannerEnvelope(_PlannerEnvelope):
    """Gemini wire envelope; converted independently to canonical state."""


class PlannerValidationStatus(StrEnum):
    PASSED = "passed"
    PROVIDER_ERROR = "provider_error"
    WIRE_INVALID = "wire_invalid"
    CANONICAL_INVALID = "canonical_invalid"
    POLICY_REJECTED = "policy_rejected"


class ProviderPlanError(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    CONFIG = "config"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER_ERROR = "server_error"
    INVALID_SCHEMA = "invalid_schema"
    GENERATED_OUTPUT_SCHEMA_MISMATCH = "generated_output_schema_mismatch"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    MODEL_REFUSAL = "model_refusal"
    SAFETY_REFUSAL = "safety_refusal"
    UNKNOWN_BAD_REQUEST = "unknown_bad_request"
    MALFORMED_RESPONSE = "malformed_response"
    CIRCUIT_OPEN = "circuit_open"
    DEADLINE = "deadline"
    INTERNAL = "internal"


class SafeProviderErrorType(StrEnum):
    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    RATE_LIMIT = "rate_limit_error"
    SERVER = "server_error"
    MODEL_REFUSAL = "model_refusal"
    SAFETY_REFUSAL = "safety_refusal"
    UNKNOWN = "unknown_provider_error"


class SafeProviderErrorCode(StrEnum):
    INVALID_SCHEMA = "invalid_schema"
    JSON_SCHEMA_INVALID = "json_schema_invalid"
    JSON_VALIDATE_FAILED = "json_validate_failed"
    GENERATED_OUTPUT_SCHEMA_MISMATCH = "generated_output_schema_mismatch"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    MODEL_REFUSAL = "model_refusal"
    SAFETY_REFUSAL = "safety_refusal"
    INVALID_API_KEY = "invalid_api_key"
    INSUFFICIENT_QUOTA = "insufficient_quota"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    SERVER_ERROR = "server_error"
    UNKNOWN = "unknown_provider_error"


SafeProviderErrorParam = Literal[
    "response_format",
    "json_schema",
    "messages",
    "model",
    "reasoning_effort",
    "max_completion_tokens",
]


class ProviderErrorDiagnostic(BaseModel):
    """Bounded error metadata; raw provider text never enters this model."""

    model_config = ConfigDict(extra="forbid")

    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_error_type: SafeProviderErrorType
    provider_error_code: SafeProviderErrorCode
    parameter: SafeProviderErrorParam | None = None
    category: ProviderPlanError
    retryable: bool
    response_bytes: int = Field(ge=0)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderPlanResult(BaseModel):
    """Normalized result; raw provider data never crosses this boundary."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=30)
    model: str = Field(min_length=1, max_length=100)
    decision: PlannerDecision | None = None
    error: ProviderPlanError | None = None
    diagnostic: ProviderErrorDiagnostic | None = None
    latency_ms: int = Field(ge=0)
    schema_mode: str = Field(min_length=1, max_length=40)
    validation_status: PlannerValidationStatus

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> "ProviderPlanResult":
        if (self.decision is None) == (self.error is None):
            raise ValueError("provider result must contain one outcome")
        return self
