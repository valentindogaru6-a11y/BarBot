"""Validated models for warehouse product seeds."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.models.catalog_item import StableId
from app.models.warehouse.nomenclature import BaseUnit, WarehouseName


class SeedOperationType(StrEnum):
    WRITEOFF = "writeoff"
    TRANSFER = "transfer"


class WarehouseRequiredRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["recommended", "manual"]
    unit: BaseUnit
    schedule: Literal["daily"]
    quantity: str | None = None

    @model_validator(mode="after")
    def validate_quantity(self) -> "WarehouseRequiredRule":
        if self.mode == "manual":
            if self.quantity is not None:
                raise ValueError("manual rule must not contain quantity")
            return self
        if self.quantity is None:
            raise ValueError("recommended rule requires quantity")
        try:
            quantity = Decimal(self.quantity)
        except InvalidOperation as exc:
            raise ValueError("required quantity must be decimal text") from exc
        if quantity <= 0:
            raise ValueError("required quantity must be positive")
        return self


class WarehouseOwnerMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_rule: WarehouseRequiredRule


class WarehouseSeedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_id: StableId
    official_name: WarehouseName
    normalized_name: WarehouseName
    base_unit: BaseUnit
    is_active: StrictBool
    allowed_operation_types: list[SeedOperationType]
    aliases: list[WarehouseName]
    source_candidate_ids: list[str] = Field(min_length=1)
    review_status: Literal["approved", "review_required", "owner_required"]
    warnings: list[str]
    owner_metadata: WarehouseOwnerMetadata | None = None

    @model_validator(mode="after")
    def validate_product(self) -> "WarehouseSeedProduct":
        if set(self.allowed_operation_types) != set(SeedOperationType):
            raise ValueError("both writeoff and transfer are required")
        if len(self.allowed_operation_types) != len(set(self.allowed_operation_types)):
            raise ValueError("operation types must be unique")
        if (self.review_status == "owner_required") != (self.owner_metadata is not None):
            raise ValueError("owner metadata is required only for owner products")
        if self.owner_metadata and self.owner_metadata.required_rule.unit != self.base_unit:
            raise ValueError("required rule unit must match base unit")
        if not self.is_active:
            raise ValueError("all approved warehouse seed products must be active")
        return self


class WarehouseSeedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clean_candidates_active: Literal[True]
    review_required_active: Literal[True]
    owner_required_active: Literal[True]


class WarehouseProductsSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1]
    source_candidate_format_version: Literal[1]
    source_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: WarehouseSeedPolicy
    products: list[WarehouseSeedProduct] = Field(min_length=1)


class WarehouseSeedImportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    create: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    applied: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.conflicts and not self.errors
