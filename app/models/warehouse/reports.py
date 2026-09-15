"""Validated values exchanged by warehouse report services."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.models.warehouse.nomenclature import BaseUnit


class WarehouseReportStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class WarehouseReportSheet(StrEnum):
    WRITEOFF = "writeoff"
    TRANSFER = "transfer"


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
    business_date: str = Field(min_length=10, max_length=10)


class WarehouseReportArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    relative_file_path: str
    file_sha256: str = Field(min_length=64, max_length=64)
    file_size_bytes: int = Field(gt=0)
