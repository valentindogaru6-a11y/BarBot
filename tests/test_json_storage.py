"""Tests for read-only JSON catalog storage."""

import json
from pathlib import Path

import pytest

from app.storage.json_storage import (
    CatalogFileNotFoundError,
    CatalogJSONDecodeError,
    CatalogValidationError,
    JsonCatalogStorage,
)


def test_loads_repository_catalog_successfully() -> None:
    catalog_path = Path(__file__).resolve().parents[1] / "data" / "catalog.json"

    catalog = JsonCatalogStorage(catalog_path).load()

    assert catalog.version == 1
    assert [category.id for category in catalog.categories] == [
        "cocktails",
        "signature_tea",
        "coffee",
        "wine",
        "polufabrikaty_p_f",
        "bar_alcohol",
        "soft_drinks",
        "desserts",
        "classic_tea",
        "new_lemonades",
    ]
    assert [
        subcategory.id
        for subcategory in catalog.categories[0].subcategories
    ] == [
        "signature",
        "classic",
        "shots",
        "old_cocktails",
        "old_classics",
        "non_alcoholic",
        "hot_cocktails",
    ]


def test_missing_file_has_clear_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    with pytest.raises(CatalogFileNotFoundError, match="не найден"):
        JsonCatalogStorage(missing_path).load()


def test_malformed_json_has_clear_error(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text('{"version": 1,', encoding="utf-8")

    with pytest.raises(CatalogJSONDecodeError, match="Некорректный JSON"):
        JsonCatalogStorage(catalog_path).load()


def test_invalid_structure_has_clear_error(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps({"version": 0, "categories": "not-a-list"}),
        encoding="utf-8",
    )

    with pytest.raises(CatalogValidationError, match="не прошла валидацию"):
        JsonCatalogStorage(catalog_path).load()
