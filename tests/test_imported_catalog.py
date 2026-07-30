"""Integrity tests for imported production catalog data."""

from pathlib import Path

import pytest

from app.keyboards.callbacks import ItemCallback
from app.models.catalog import Catalog
from app.storage.json_storage import JsonCatalogStorage


@pytest.fixture(scope="module")
def imported_catalog() -> Catalog:
    path = Path(__file__).resolve().parents[1] / "data" / "catalog.json"
    return JsonCatalogStorage(path).load()


def _all_items(catalog: Catalog):
    for category in catalog.categories:
        yield from category.items
        for subcategory in category.subcategories:
            yield from subcategory.items


def test_imported_catalog_counts(imported_catalog: Catalog) -> None:
    assert len(imported_catalog.categories) == 1
    assert len(imported_catalog.categories[0].subcategories) == 4
    assert len(list(_all_items(imported_catalog))) == 24
    counts = {
        subcategory.id: len(subcategory.items)
        for subcategory in imported_catalog.categories[0].subcategories
    }
    assert counts == {
        "signature": 15,
        "classic": 8,
        "non_alcoholic": 1,
        "methods": 0,
    }


def test_every_imported_item_has_no_photo(imported_catalog: Catalog) -> None:
    assert all(
        item.photo_file_id is None
        for item in _all_items(imported_catalog)
    )


def test_all_catalog_ids_are_globally_unique(
    imported_catalog: Catalog,
) -> None:
    ids = []
    for category in imported_catalog.categories:
        ids.append(category.id)
        for subcategory in category.subcategories:
            ids.append(subcategory.id)
        ids.extend(item.id for item in category.items)
        ids.extend(
            item.id
            for subcategory in category.subcategories
            for item in subcategory.items
        )

    assert len(ids) == len(set(ids))


def test_demo_item_was_removed(imported_catalog: Catalog) -> None:
    item_ids = {item.id for item in _all_items(imported_catalog)}

    assert "test-passion-cocktail" not in item_ids
    assert all("тестов" not in item.name.casefold() for item in _all_items(imported_catalog))


def test_all_imported_items_have_empty_steps(
    imported_catalog: Catalog,
) -> None:
    assert all(not item.steps for item in _all_items(imported_catalog))


def test_imported_ingredient_count(imported_catalog: Catalog) -> None:
    assert sum(
        len(item.ingredients)
        for item in _all_items(imported_catalog)
    ) == 118


def test_all_imported_item_callbacks_fit_telegram_limit(
    imported_catalog: Catalog,
) -> None:
    category = imported_catalog.categories[0]
    callbacks = [
        ItemCallback(
            category_id=category.id,
            subcategory_id=subcategory.id,
            item_id=item.id,
        ).pack()
        for subcategory in category.subcategories
        for item in subcategory.items
    ]

    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
