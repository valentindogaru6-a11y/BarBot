"""Display-only DTOs: no business handles, SQL, recipients or actor identifiers."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid')


class UserDisplay(Closed):
    name: str = Field(max_length=500)
    active: bool
    role: Literal['super_admin','admin','bartender','accountant','user','blocked'] | None = None


class OperationDisplay(Closed):
    type: str = Field(max_length=40)
    product: str = Field(max_length=500)
    quantity: str = Field(max_length=100)


class ReportDisplay(Closed):
    business_date: str = Field(max_length=20)
    version: int = Field(ge=0)
    status: str = Field(max_length=50)


class RecipientDisplay(Closed):
    title: str = Field(max_length=500)
    active: bool


class ScheduleDisplay(RecipientDisplay):
    type: str = Field(max_length=50)


class ReadData(Closed):
    attendance_open: bool | None = None
    users: list[UserDisplay] | None = Field(default=None,max_length=100)
    operations: list[OperationDisplay] | None = Field(default=None,max_length=50)
    total: int | None = Field(default=None,ge=0)
    resolved: int | None = Field(default=None,ge=0)
    employee: str | None = Field(default=None,max_length=500)
    location: str | None = Field(default=None,max_length=200)
    business_date: str | None = Field(default=None,max_length=20)
    version: int | None = Field(default=None,ge=0)
    status: str | None = Field(default=None,max_length=50)
    reports: list[ReportDisplay] | None = Field(default=None,max_length=100)
    recipients: list[RecipientDisplay] | None = Field(default=None,max_length=100)
    enabled: bool | None = None
    first_minutes: int | None = Field(default=None,ge=0)
    repeat_minutes: int | None = Field(default=None,ge=0)
    maximum: int | None = Field(default=None,ge=0)
    schedules: list[ScheduleDisplay] | None = Field(default=None,max_length=50)
    groups: dict[str,list[str]] | None = None


class ReadDispatch(Closed):
    capability_id: str = Field(min_length=1,max_length=100)
    message: str = Field(max_length=4000)
    ui_action: Literal['main_menu'] | None = None
    data: ReadData | None = None
    # Validated against AssistantToolAction, including every nested catalog
    # model, by read_broker.decode_action before exposure to a renderer.
    tool_action: dict | None = None
