"""Validated warehouse location, product, and settings models."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.catalog_item import StableId

WarehouseName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class BaseUnit(StrEnum):
    KG = "kg"
    L = "l"
    PCS = "pcs"


class ProductOperationType(StrEnum):
    PRODUCT_WRITEOFF = "product_writeoff"
    TRANSFER = "transfer"
    DEVELOPMENT = "development"


class WarehouseLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    stable_id: StableId
    name: WarehouseName
    normalized_name: WarehouseName
    is_active: bool
    can_be_source: bool
    can_be_destination: bool
    created_at_utc: str
    created_by: int | None = Field(default=None, gt=0)
    updated_at_utc: str
    updated_by: int | None = Field(default=None, gt=0)
    revision: int = Field(gt=0)


class WarehouseLocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WarehouseName
    is_active: bool = True
    can_be_source: bool = True
    can_be_destination: bool = True


class WarehouseLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WarehouseName
    is_active: bool
    can_be_source: bool
    can_be_destination: bool
    expected_revision: int = Field(gt=0)


class WarehouseProductAlias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    alias: WarehouseName
    normalized_alias: WarehouseName
    created_at_utc: str
    created_by: int | None = Field(default=None, gt=0)


class WarehouseProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    stable_id: StableId
    official_name: WarehouseName
    normalized_name: WarehouseName
    base_unit: BaseUnit
    is_active: bool
    created_at_utc: str
    created_by: int | None = Field(default=None, gt=0)
    updated_at_utc: str
    updated_by: int | None = Field(default=None, gt=0)
    revision: int = Field(gt=0)
    aliases: tuple[WarehouseProductAlias, ...] = ()
    operation_types: frozenset[ProductOperationType]


class WarehouseProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    official_name: WarehouseName
    base_unit: BaseUnit
    aliases: list[WarehouseName] = Field(default_factory=list)
    operation_types: set[ProductOperationType]
    is_active: bool = True


class WarehouseProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    official_name: WarehouseName
    base_unit: BaseUnit
    aliases: list[WarehouseName]
    operation_types: set[ProductOperationType]
    is_active: bool
    expected_revision: int = Field(gt=0)


class WarehouseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1, le=1)
    timezone: str
    cutoff_local_time: str
    default_source_location_id: int = Field(gt=0)
    updated_at_utc: str
    updated_by: int | None = Field(default=None, gt=0)


class WarehouseSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str
    cutoff_local_time: str
    default_source_location_id: int = Field(gt=0)
