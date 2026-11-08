"""Provider-boundary planner contracts with no execution authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.assistant_planner import AssistantDialogueAct, PlannerDecision


class PlannerRouteDomain(StrEnum):
    CATALOG = "catalog"
    WAREHOUSE = "warehouse"
    ATTENDANCE = "attendance"
    REPORTS = "reports"
    USERS = "users"
    RECIPIENTS = "recipients"
    REMINDERS = "reminders"
    SCHEDULES = "schedules"
    NAVIGATION = "navigation"
    GENERAL = "general"


class PlannerRouteActionCode(StrEnum):
    SEARCH = "search"
    OPEN = "open"
    LIST = "list"
    EXPLAIN = "explain"
    CREATE = "create"
    EDIT = "edit"
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    GENERATE = "generate"
    SEND = "send"
    START = "start"
    CLOSE = "close"
    CANCEL = "cancel"
    CONFIRM = "confirm"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class PlannerRouteEnvelope(BaseModel):
    """Compact Stage 1 output with no arguments, authority or stable IDs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dialogue_act: AssistantDialogueAct
    domain: PlannerRouteDomain
    capability_id: str = Field(min_length=1, max_length=100)
    target_index: int = Field(ge=-1, le=4)
    action_code: PlannerRouteActionCode
    needs_clarification: bool
    confidence_scaled: int = Field(ge=0, le=10_000)


class GroqPlannerRouteEnvelope(PlannerRouteEnvelope):
    """Groq-specific Stage 1 wire boundary."""


class GeminiPlannerRouteEnvelope(PlannerRouteEnvelope):
    """Gemini-specific Stage 1 wire boundary."""


class CatalogIngredientOperation(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


class CatalogIngredientArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: CatalogIngredientOperation
    ingredient_name: str = Field(max_length=200)
    quantity_text: str = Field(max_length=50)
    unit_text: str = Field(max_length=30)
    replacement_name: str = Field(max_length=200)


class WarehouseWriteoffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_text: str = Field(max_length=200)
    quantity_text: str = Field(max_length=50)
    unit_text: str = Field(max_length=30)
    reason_text: str = Field(max_length=500)


class WarehouseTransferArguments(WarehouseWriteoffArguments):
    destination_text: str = Field(max_length=200)


class AttendanceForceCloseArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_index: int = Field(ge=-1, le=4)
    reason_text: str = Field(max_length=500)


class PlannerValidationStatus(StrEnum):
    PASSED = "passed"
    PROVIDER_ERROR = "provider_error"
    WIRE_INVALID = "wire_invalid"
    CANONICAL_INVALID = "canonical_invalid"
    POLICY_REJECTED = "policy_rejected"


class ProviderPlanError(StrEnum):
    BUSY = "busy"
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
