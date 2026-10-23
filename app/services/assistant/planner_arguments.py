"""Bounded Stage 2 argument contracts and deterministic local extraction."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Mapping

from pydantic import BaseModel, ValidationError

from app.models.assistant_provider_planner import (
    AttendanceForceCloseArguments,
    CatalogIngredientArguments,
    CatalogIngredientOperation,
    WarehouseTransferArguments,
    WarehouseWriteoffArguments,
)
from app.services.assistant.bar_language import normalize_bar_text, parse_bar_request


_QUANTITY_UNIT_RE = re.compile(
    r"\b(?P<quantity>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>кг|г|гр|л|мл|шт|килограмм\w*|грамм\w*|литр\w*|миллилитр\w*)\b"
)
_CATALOG_CHANGE_RE = re.compile(
    r"^\s*(?P<operation>добавь|добавить|удали|удалить|убери|замени|заменить)\s+"
    r"(?P<body>.+?)\s*$",
    re.IGNORECASE,
)


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def extract_catalog_ingredient_arguments(text: str) -> CatalogIngredientArguments | None:
    """Extract simple ingredient changes locally; return None when ambiguous."""

    match = _CATALOG_CHANGE_RE.match(text)
    if match is None:
        return None
    operation_word = normalize_bar_text(match.group("operation"))
    operation = (
        CatalogIngredientOperation.ADD
        if operation_word.startswith("добав")
        else CatalogIngredientOperation.REMOVE
        if operation_word.startswith(("удал", "убер"))
        else CatalogIngredientOperation.REPLACE
    )
    body = " ".join(match.group("body").split())
    replacement = ""
    if operation is CatalogIngredientOperation.REPLACE:
        parts = re.split(r"\s+на\s+", body, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            return None
        body, replacement = parts
    amount = _QUANTITY_UNIT_RE.search(normalize_bar_text(body))
    quantity = amount.group("quantity").replace(",", ".") if amount else ""
    unit = amount.group("unit") if amount else ""
    ingredient = body
    if amount:
        ingredient = (body[: amount.start()] + body[amount.end() :]).strip()
    ingredient = re.sub(r"^(?:туда|в\s+состав)\s+", "", ingredient, flags=re.IGNORECASE)
    return CatalogIngredientArguments(
        operation=operation,
        ingredient_name=" ".join(ingredient.split())[:200],
        quantity_text=quantity,
        unit_text=unit,
        replacement_name=" ".join(replacement.split())[:200],
    )


def extract_warehouse_arguments(
    capability_id: str, text: str
) -> WarehouseWriteoffArguments | WarehouseTransferArguments | None:
    parsed = parse_bar_request(text)
    if capability_id == "warehouse.prepare_writeoff" and parsed.action == "writeoff":
        return WarehouseWriteoffArguments(
            product_text=parsed.entity,
            quantity_text=_decimal_text(parsed.quantity),
            unit_text=parsed.unit,
            reason_text=parsed.comment,
        )
    if capability_id == "warehouse.prepare_transfer" and parsed.action == "transfer":
        return WarehouseTransferArguments(
            product_text=parsed.entity,
            quantity_text=_decimal_text(parsed.quantity),
            unit_text=parsed.unit,
            reason_text=parsed.comment,
            destination_text=parsed.destination,
        )
    return None


def local_stage_two_arguments(capability_id: str, text: str) -> dict[str, str] | None:
    """Return canonical local arguments, or None when a provider extractor is needed."""

    if capability_id == "catalog.search":
        parsed = parse_bar_request(text)
        query = (parsed.entity or text).strip()
        return {"query": query[:500]} if query else None
    if capability_id == "catalog.manage":
        extracted = extract_catalog_ingredient_arguments(text)
        if extracted is None:
            return None
        return {
            "reason": extracted.model_dump_json(),
        }
    warehouse = extract_warehouse_arguments(capability_id, text)
    if warehouse is None:
        return None
    values = warehouse.model_dump()
    return {
        "product": values["product_text"],
        "quantity": values["quantity_text"],
        "unit": values["unit_text"],
        **(
            {"destination": values["destination_text"]}
            if "destination_text" in values
            else {}
        ),
        "comment": values["reason_text"],
    }


def argument_wire_schema(capability_id: str) -> Mapping[str, object]:
    """Return one closed capability-specific Stage 2 schema."""

    if capability_id == "catalog.manage":
        properties: dict[str, object] = {
            "operation": {"type": "string", "enum": ["add", "remove", "replace"]},
            "ingredient_name": {"type": "string", "maxLength": 200},
            "quantity_text": {"type": "string", "maxLength": 50},
            "unit_text": {"type": "string", "maxLength": 30},
            "replacement_name": {"type": "string", "maxLength": 200},
        }
    elif capability_id == "warehouse.prepare_writeoff":
        properties = {
            "product_text": {"type": "string", "maxLength": 200},
            "quantity_text": {"type": "string", "maxLength": 50},
            "unit_text": {"type": "string", "maxLength": 30},
            "reason_text": {"type": "string", "maxLength": 500},
        }
    elif capability_id == "warehouse.prepare_transfer":
        properties = {
            "product_text": {"type": "string", "maxLength": 200},
            "quantity_text": {"type": "string", "maxLength": 50},
            "unit_text": {"type": "string", "maxLength": 30},
            "reason_text": {"type": "string", "maxLength": 500},
            "destination_text": {"type": "string", "maxLength": 200},
        }
    elif capability_id == "attendance.force_close":
        properties = {
            "target_index": {"type": "integer", "minimum": -1, "maximum": 4},
            "reason_text": {"type": "string", "maxLength": 500},
        }
    else:
        raise ValueError("no Stage 2 schema for capability")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def validate_argument_payload(capability_id: str, payload: object) -> BaseModel:
    models: dict[str, type[BaseModel]] = {
        "catalog.manage": CatalogIngredientArguments,
        "warehouse.prepare_writeoff": WarehouseWriteoffArguments,
        "warehouse.prepare_transfer": WarehouseTransferArguments,
        "attendance.force_close": AttendanceForceCloseArguments,
    }
    model = models.get(capability_id)
    if model is None:
        raise ValueError("no Stage 2 validator for capability")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("invalid Stage 2 argument payload") from exc
