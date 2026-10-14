"""Strict data contracts for the bounded AI assistant pilot."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantIntentName(StrEnum):
    CAPABILITY = "capability"
    FIND_TECH_CARD = "find_tech_card"
    PREPARE_WRITEOFF = "prepare_writeoff"
    PREPARE_TRANSFER = "prepare_transfer"
    CLARIFY = "clarify"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    GENERAL_CHAT = "general_chat"
    WEB_SEARCH = "web_search"
    LIST_OPEN_ATTENDANCE_SESSIONS = "list_open_attendance_sessions"
    GET_CURRENT_SHIFT_SUMMARY = "get_current_shift_summary"


class AssistantDomain(StrEnum):
    UNSPECIFIED = "unspecified"
    CATALOG = "catalog"
    WAREHOUSE = "warehouse"
    ATTENDANCE = "attendance"
    EMPLOYEES = "employees"
    REPORTS = "reports"
    ADMINISTRATION = "administration"
    GENERAL = "general"


class AssistantRequestedKind(StrEnum):
    UNSPECIFIED = "unspecified"
    PREPARATION = "preparation"
    TECH_CARD = "tech_card"
    INGREDIENT = "ingredient"
    CATEGORY = "category"


class AssistantRelation(StrEnum):
    UNSPECIFIED = "unspecified"
    PREPARATION_FOR = "preparation_for"
    INGREDIENTS_OF = "ingredients_of"
    SIMILAR_TO = "similar_to"
    DIRECT_LOOKUP = "direct_lookup"


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
    answer_text: str | None = Field(default=None, max_length=4_000)
    needs_web: bool = False
    capability_id: str | None = Field(default=None, max_length=100)
    domain: AssistantDomain = AssistantDomain.UNSPECIFIED
    requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED
    relation: AssistantRelation = AssistantRelation.UNSPECIFIED
    relation_target: str | None = Field(default=None, max_length=300)
    parent_entity_id: str | None = Field(default=None, max_length=300)
    selected_entity_id: str | None = Field(default=None, max_length=300)
    evidence: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_shape(self) -> "AssistantIntent":
        query_intents = {
            AssistantIntentName.FIND_TECH_CARD,
            AssistantIntentName.NOT_FOUND,
            AssistantIntentName.WEB_SEARCH,
        }
        if self.intent is AssistantIntentName.CAPABILITY:
            if not self.capability_id:
                raise ValueError("capability intent requires capability_id")
            if (
                self.items
                or self.query
                or self.clarification
                or self.answer_text
                or self.needs_web
                or self.domain is not AssistantDomain.UNSPECIFIED
                or self.requested_kind is not AssistantRequestedKind.UNSPECIFIED
                or self.relation is not AssistantRelation.UNSPECIFIED
                or self.relation_target
                or self.parent_entity_id
                or self.selected_entity_id
                or self.evidence
            ):
                raise ValueError("capability intent contains unsupported fields")
            return self
        catalog_metadata = bool(
            self.requested_kind is not AssistantRequestedKind.UNSPECIFIED
            or self.relation is not AssistantRelation.UNSPECIFIED
            or self.relation_target
            or self.parent_entity_id
            or self.selected_entity_id
        )
        if catalog_metadata and self.intent not in {
            AssistantIntentName.FIND_TECH_CARD,
            AssistantIntentName.NOT_FOUND,
        }:
            raise ValueError("catalog relation fields require a catalog intent")
        if (
            self.relation is AssistantRelation.UNSPECIFIED
            and self.relation_target is not None
        ):
            raise ValueError("relation_target requires relation")
        if any(len(value) > 100 for value in self.evidence):
            raise ValueError("evidence marker is too long")
        if self.capability_id is not None:
            raise ValueError("capability_id is only valid for capability intent")
        if self.intent not in query_intents and self.query is not None:
            raise ValueError("query is not valid for this intent")
        if (
            self.intent is not AssistantIntentName.CLARIFY
            and self.clarification is not None
        ):
            raise ValueError("clarification is only valid for clarify")
        if self.intent not in {
            AssistantIntentName.PREPARE_WRITEOFF,
            AssistantIntentName.PREPARE_TRANSFER,
        } and self.items:
            raise ValueError("items are only valid for warehouse intents")
        if self.intent in {
            AssistantIntentName.FIND_TECH_CARD,
            AssistantIntentName.NOT_FOUND,
        } and not self.query:
            raise ValueError(f"{self.intent.value} requires query")
        if self.intent in {
            AssistantIntentName.PREPARE_WRITEOFF,
            AssistantIntentName.PREPARE_TRANSFER,
        } and not self.items:
            raise ValueError("warehouse intent requires items")
        if self.intent is AssistantIntentName.CLARIFY and not self.clarification:
            raise ValueError("clarify requires clarification")
        if self.intent is AssistantIntentName.GENERAL_CHAT:
            if (
                not self.answer_text
                or self.needs_web
                or self.query
                or self.clarification
            ):
                raise ValueError("general_chat requires answer_text without web")
        elif self.answer_text is not None:
            raise ValueError("answer_text is only valid for general_chat")
        if self.intent is AssistantIntentName.WEB_SEARCH:
            if not self.query or not self.needs_web or self.clarification:
                raise ValueError("web_search requires query and needs_web")
        elif self.needs_web:
            raise ValueError("needs_web is only valid for web_search")
        return self
