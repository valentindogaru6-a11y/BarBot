"""Validated records used by the warehouse operations backend."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.warehouse.nomenclature import BaseUnit


class WarehouseOperationType(StrEnum):
    WRITEOFF = "writeoff"
    TRANSFER = "transfer"


class ShiftStatus(StrEnum):
    OPEN = "open"
    WAITING_REQUIRED = "waiting_required"
    CLOSING = "closing"
    CLOSED = "closed"
    CORRECTION_OPEN = "correction_open"


class IntakeBatchStatus(StrEnum):
    NEEDS_RESOLUTION = "needs_resolution"
    READY = "ready"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class IntakeLineStatus(StrEnum):
    PARSED = "parsed"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    RESOLVED = "resolved"
    COMMITTED = "committed"
    CANCELLED = "cancelled"


class OperationOrigin(StrEnum):
    MESSAGE = "message"
    MANUAL = "manual"
    REQUIRED_RULE = "required_rule"


class ProductCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    official_name: str
    score_scaled: int = Field(ge=0, le=10_000)
    rank: int = Field(gt=0)


class WarehouseShift(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    source_location_id: int = Field(gt=0)
    business_date: str
    timezone_snapshot: str
    cutoff_snapshot: str
    status: ShiftStatus
    started_at_utc: str
    scheduled_close_at_utc: str
    closed_at_utc: str | None = None
    closed_by: int | None = Field(default=None, gt=0)
    revision: int = Field(gt=0)
    created_at_utc: str
    updated_at_utc: str


class IntakeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, gt=0)
    line_number: int = Field(gt=0)
    raw_line: str
    status: IntakeLineStatus
    product_text: str | None = None
    product_id: int | None = Field(default=None, gt=0)
    input_quantity_text: str | None = None
    input_unit_text: str | None = None
    quantity_scaled: int | None = Field(default=None, gt=0)
    base_unit: BaseUnit | None = None
    reason_text: str | None = None
    comment_text: str | None = None
    destination_text: str | None = None
    destination_location_id: int | None = Field(default=None, gt=0)
    error_code: str | None = None
    candidates: tuple[ProductCandidate, ...] = ()


class IntakeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    shift_id: int = Field(gt=0)
    actor_user_id: int = Field(gt=0)
    source_chat_id: int
    source_message_id: int
    operation_type: WarehouseOperationType
    raw_text: str
    status: IntakeBatchStatus
    created_at_utc: str
    updated_at_utc: str
    committed_at_utc: str | None = None
    lines: tuple[IntakeLine, ...]


class WarehouseOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    shift_id: int = Field(gt=0)
    operation_type: WarehouseOperationType
    product_id: int = Field(gt=0)
    product_name_snapshot: str
    quantity_scaled: int = Field(gt=0)
    base_unit: BaseUnit
    input_quantity_text: str
    input_unit_text: str | None = None
    source_location_id: int = Field(gt=0)
    destination_location_id: int | None = Field(default=None, gt=0)
    reason_text: str | None = None
    comment_text: str | None = None
    raw_text: str
    author_user_id: int = Field(gt=0)
    occurred_at_utc: str
    intake_line_id: int | None = Field(default=None, gt=0)
    origin: OperationOrigin
    is_deleted: bool
    deleted_at_utc: str | None = None
    deleted_by: int | None = Field(default=None, gt=0)
    deletion_reason: str | None = None
    created_at_utc: str
    updated_at_utc: str
    revision: int = Field(gt=0)
