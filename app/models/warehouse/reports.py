"""Validated values exchanged by warehouse report services."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.warehouse.nomenclature import BaseUnit


class WarehouseReportStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class WarehouseReportSheet(StrEnum):
    WRITEOFF = "writeoff"
    TRANSFER = "transfer"
    DEVELOPMENT = "development"


class WarehouseReportLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_type: WarehouseReportSheet
    row_number: int = Field(gt=0)
    product_id: int = Field(gt=0)
    product_name: str = Field(min_length=1)
    quantity_scaled: int = Field(gt=0)
    base_unit: BaseUnit
    reason_or_comment: str | None = None
    source_location_name: str = Field(min_length=1)
    destination_location_name: str | None = None
    development_title: str | None = Field(default=None, max_length=200)
    development_group_id: int | None = Field(default=None, gt=0)
    business_date: str = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_sheet_metadata(self) -> "WarehouseReportLine":
        if self.sheet_type is WarehouseReportSheet.DEVELOPMENT:
            if not (self.development_title or "").strip():
                raise ValueError("development title is required")
            if self.development_group_id is None:
                raise ValueError("development group is required")
            if self.destination_location_name is not None:
                raise ValueError("development cannot have a destination")
        elif self.development_title is not None or self.development_group_id is not None:
            raise ValueError("development metadata is only valid for development")
        return self


class WarehouseReportArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    relative_file_path: str
    file_sha256: str = Field(min_length=64, max_length=64)
    file_size_bytes: int = Field(gt=0)
