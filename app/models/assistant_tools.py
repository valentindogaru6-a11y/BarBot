"""Strict contracts for the assistant's read-only local tools."""

from pydantic import BaseModel, ConfigDict, Field


class SearchTechCardsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)


class GetTechCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    card_id: str = Field(min_length=1, max_length=300)


class TechCardToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    name: str
    category: str
    subcategory: str | None = None


class OpenAttendanceSessionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAttendanceSessionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee: str
    username: str | None = None
    location: str
    business_date: str
    opened_at_utc: str
    duration_seconds: int = Field(ge=0)
    status: str


class CurrentShiftSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurrentShiftSummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exists: bool
    location: str
    business_date: str
    status: str
