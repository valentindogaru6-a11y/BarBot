"""Strict data contracts for the bounded AI assistant pilot."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantIntentName(StrEnum):
    FIND_TECH_CARD = "find_tech_card"
    PREPARE_WRITEOFF = "prepare_writeoff"
    PREPARE_TRANSFER = "prepare_transfer"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class AssistantIntentItem(BaseModel):
    """One user-supplied warehouse line without internal identifiers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_text: str | None = Field(default=None, max_length=200)
    quantity_text: str | None = Field(default=None, max_length=50)
    unit_text: str | None = Field(default=None, max_length=30)
    destination_text: str | None = Field(default=None, max_length=200)
    comment_text: str | None = Field(default=None, max_length=500)


class AssistantIntent(BaseModel):
    """Provider output accepted by the local allowlist."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: AssistantIntentName
    confidence: int = Field(default=0, ge=0, le=10_000)
    items: list[AssistantIntentItem] = Field(default_factory=list, max_length=20)
    query: str | None = Field(default=None, max_length=200)
    clarification: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_shape(self) -> "AssistantIntent":
        if self.intent is AssistantIntentName.FIND_TECH_CARD and not self.query:
            raise ValueError("find_tech_card requires query")
        if self.intent in {
            AssistantIntentName.PREPARE_WRITEOFF,
            AssistantIntentName.PREPARE_TRANSFER,
        } and not self.items:
            raise ValueError("warehouse intent requires items")
        if self.intent is AssistantIntentName.CLARIFY and not self.clarification:
            raise ValueError("clarify requires clarification")
        return self
