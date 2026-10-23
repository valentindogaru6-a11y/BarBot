import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.assistant.planner_arguments import (
    argument_wire_schema, extract_catalog_ingredient_arguments,
    extract_warehouse_arguments, local_stage_two_arguments, validate_argument_payload,
)


@pytest.mark.parametrize("text,operation,name", [
    ("добавь туда 100 мл апельсинового фреша", "add", "апельсинового фреша"),
    ("удали сахар", "remove", "сахар"),
    ("замени лимон на лайм", "replace", "лимон"),
])
def test_catalog_ingredient_extraction(text, operation, name):
    result = extract_catalog_ingredient_arguments(text)
    assert result is not None
    assert result.operation.value == operation and result.ingredient_name == name


@pytest.mark.parametrize("capability,text,quantity,destination", [
    ("warehouse.prepare_writeoff", "спиши апельсин 200 грамм", Decimal("200"), None),
    ("warehouse.prepare_transfer", "перемести сахар 1 кг на кухню", Decimal("1"), "кухня"),
])
def test_warehouse_arguments_use_decimal_parser(capability, text, quantity, destination):
    parsed = extract_warehouse_arguments(capability, text)
    assert parsed is not None and Decimal(parsed.quantity_text) == quantity
    if destination is not None: assert parsed.destination_text == destination


@pytest.mark.parametrize("capability", ["catalog.manage", "warehouse.prepare_writeoff",
                                         "warehouse.prepare_transfer", "attendance.force_close"])
def test_stage_two_schemas_are_closed_and_bounded(capability):
    schema = argument_wire_schema(capability)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_argument_validation_rejects_unknown_fields():
    with pytest.raises(ValueError):
        validate_argument_payload("warehouse.prepare_writeoff", {
            "product_text": "апельсин", "quantity_text": "1", "unit_text": "кг",
            "reason_text": "", "confirmed": True})


def test_catalog_search_arguments_are_local():
    result = local_stage_two_arguments("catalog.search", "найди пф для облепихи")
    assert result and result["query"]


def test_missing_and_incompatible_warehouse_fields_remain_unresolved():
    missing = extract_warehouse_arguments("warehouse.prepare_writeoff", "спиши апельсин")
    incompatible = extract_warehouse_arguments(
        "warehouse.prepare_writeoff", "спиши апельсин 2 ведра"
    )
    assert missing is not None and missing.quantity_text == "" and missing.unit_text == ""
    assert incompatible is not None and incompatible.unit_text == ""


def test_versioned_two_stage_corpus_separates_route_and_arguments():
    payload = json.loads((Path(__file__).parent / "fixtures/assistant_planner_corpus_v2.json").read_text())
    assert payload["version"] == "bar-jarvis-two-stage-v2"
    assert len(payload["stage1_cases"]) == 9
    assert len(payload["stage2_cases"]) == 4
    assert "no live accuracy claim" in payload["methodology"].casefold()
