"""Strict contracts for the allowlisted BarBot capability layer."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CapabilityRisk(IntEnum):
    READ = 0
    FLOW = 1
    MUTATION = 2
    HIGH = 3


class ConfirmationPolicy(StrEnum):
    NONE = "none"
    PREVIEW = "preview"
    EXPLICIT = "explicit"
    REASON_AND_EXPLICIT = "reason_and_explicit"


class CapabilityStatus(StrEnum):
    COLLECTING = "collecting"
    READY = "ready"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    CONFLICTED = "conflicted"


class NoCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)


class EntityCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    entity_ref: str = Field(min_length=1, max_length=300)
    expected_revision: int | None = Field(default=None, ge=0)


class WarehouseCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product: str | None = Field(default=None, max_length=200)
    quantity: str | None = Field(default=None, max_length=50)
    unit: str | None = Field(default=None, max_length=30)
    destination: str | None = Field(default=None, max_length=200)
    comment: str | None = Field(default=None, max_length=500)


class PeriodCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    period: str = Field(min_length=1, max_length=50)


class MutationCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_ref: str | None = Field(default=None, max_length=300)
    expected_revision: int | None = Field(default=None, ge=0)
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("values")
    @classmethod
    def bounded_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 30:
            raise ValueError("Too many capability arguments")
        return value


class CapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: str = Field(min_length=1, max_length=50)
    message: str = Field(min_length=1, max_length=4000)
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 50:
            raise ValueError("Capability result is too large")
        return value


class ActionProposal(BaseModel):
    """Public proposal state without exposing actor/chat identifiers."""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{16,32}$")
    capability_id: str = Field(min_length=1, max_length=100)
    risk_level: CapabilityRisk
    status: CapabilityStatus
    target_type: str | None = Field(default=None, max_length=100)
    target_ref: str | None = Field(default=None, max_length=300)
    target_revision: int | None = Field(default=None, ge=0)
    arguments: dict[str, Any]
    preview: dict[str, Any]
    reason: str | None = Field(default=None, max_length=500)
    created_at_utc: str
    expires_at_utc: str
    revision: int = Field(ge=1)
