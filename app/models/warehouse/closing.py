"""Validated domain values for required write-offs and shift closing."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.warehouse.nomenclature import BaseUnit


class RequiredQuantityMode(StrEnum):
    RECOMMENDED = "recommended"
    MANUAL = "manual"


class RequirementStatus(StrEnum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    NOT_APPLICABLE = "not_applicable"


class ClosingTriggerType(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    RECOVERY = "recovery"


class ClosingSessionStatus(StrEnum):
    COLLECTING = "collecting"
    READY = "ready"
    COMMITTING = "committing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RequiredRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    quantity_mode: RequiredQuantityMode
    suggested_quantity_scaled: int | None = Field(default=None, gt=0)
    base_unit: BaseUnit
    weekdays_mask: int = Field(default=127, ge=1, le=127)
    question_text: str = Field(min_length=1, max_length=500)
    sort_order: int = 0
    allow_not_applicable: bool = True
    minimum_scaled: int | None = Field(default=None, gt=0)
    maximum_scaled: int | None = Field(default=None, gt=0)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_rule(self) -> "RequiredRuleInput":
        if self.quantity_mode is RequiredQuantityMode.RECOMMENDED:
            if self.suggested_quantity_scaled is None:
                raise ValueError("recommended rule requires suggested quantity")
        elif self.suggested_quantity_scaled is not None:
            raise ValueError("manual rule cannot contain suggested quantity")
        if (
            self.minimum_scaled is not None
            and self.maximum_scaled is not None
            and self.minimum_scaled > self.maximum_scaled
        ):
            raise ValueError("minimum cannot exceed maximum")
        return self


class RequiredRuleUpdate(RequiredRuleInput):
    expected_revision: int = Field(gt=0)
