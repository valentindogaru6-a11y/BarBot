"""Strict, bounded contracts for the Bar Jarvis decision planner."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.assistant import AssistantDomain


class AssistantDialogueAct(StrEnum):
    NEW_COMMAND = "new_command"
    CORRECTION = "correction"
    CLARIFICATION_ANSWER = "clarification_answer"
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"
    FOLLOW_UP = "follow_up"
    GENERAL_CHAT = "general_chat"


class PlannerTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    text: str = Field(max_length=1_000)


class PlannerCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    capability_id: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=1, max_length=40)
    read_only: bool
    mutation: bool
    required_arguments: tuple[str, ...] = Field(default=(), max_length=12)
    allowed_arguments: tuple[str, ...] = Field(default=(), max_length=30)


class PlannerContextSnapshot(BaseModel):
    """Redacted state: never contains actor/chat identifiers or provider data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor_role: str = Field(min_length=1, max_length=40)
    available_capability_ids: tuple[str, ...] = Field(max_length=50)
    current_domain: AssistantDomain = AssistantDomain.UNSPECIFIED
    last_successful_entity_id: str | None = Field(default=None, max_length=300)
    last_successful_entity_name: str | None = Field(default=None, max_length=500)
    candidate_entity_ids: tuple[str, ...] = Field(default=(), max_length=5)
    pending_clarification: str | None = Field(default=None, max_length=200)
    pending_action: str | None = Field(default=None, max_length=100)
    last_tool_operation: str | None = Field(default=None, max_length=100)
    last_tool_summary: str | None = Field(default=None, max_length=500)
    recent_turns: tuple[PlannerTurn, ...] = Field(default=(), max_length=6)
    revision: int = Field(default=0, ge=0)
    expires_at: float | None = Field(default=None, ge=0)


class PlannerContextUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    current_domain: AssistantDomain | None = None
    selected_entity_id: str | None = Field(default=None, max_length=300)
    selected_entity_name: str | None = Field(default=None, max_length=500)
    candidate_entity_ids: tuple[str, ...] = Field(default=(), max_length=5)
    pending_clarification: str | None = Field(default=None, max_length=200)


class PlannerDecision(BaseModel):
    """Provider-selected plan. Authority and confirmation are intentionally absent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dialogue_act: AssistantDialogueAct
    domain: AssistantDomain
    capability_id: str | None = Field(max_length=100)
    target_entity_id: str | None = Field(max_length=300)
    arguments: dict[str, Any]
    confidence: int = Field(ge=0, le=10_000)
    needs_clarification: bool
    clarification_question: str | None = Field(max_length=500)
    read_only: bool
    mutation: bool
    context_updates: PlannerContextUpdates

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20 or len(json.dumps(value, ensure_ascii=False)) > 4_000:
            raise ValueError("planner arguments exceed the bounded contract")
        return value

    @model_validator(mode="after")
    def coherent_decision(self) -> "PlannerDecision":
        if self.read_only == self.mutation:
            raise ValueError("planner decision must be read-only or mutation")
        if self.needs_clarification != bool(self.clarification_question):
            raise ValueError("clarification flag and question must agree")
        return self
