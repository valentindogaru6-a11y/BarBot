"""Integrity tests for imported production catalog data."""

import hashlib
import json
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
    assert len(imported_catalog.categories) == 5
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


def test_new_empty_categories_are_present_once(
    imported_catalog: Catalog,
) -> None:
    expected = {
        "semi_finished": ("Полуфабрикаты", "🧪", 20),
        "signature_tea": ("Фирменные чаи", "🫖", 30),
        "coffee": ("Кофейная карта", "☕", 40),
        "wine": ("Винная карта", "🍷", 50),
    }
    by_id = {category.id: category for category in imported_catalog.categories}

    assert len(by_id) == len(imported_catalog.categories)
    for category_id, (name, emoji, sort_order) in expected.items():
        category = by_id[category_id]
        assert (category.name, category.emoji, category.sort_order) == (
            name,
            emoji,
            sort_order,
        )
        assert category.description is None
        assert category.is_active
        assert category.subcategories == []
        assert category.items == []


def test_existing_cocktail_category_is_byte_semantically_unchanged(
    imported_catalog: Catalog,
) -> None:
    cocktail = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )
    payload = json.dumps(
        cocktail.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert cocktail.sort_order == 10
    assert hashlib.sha256(payload).hexdigest() == (
        "8772ab82d4bda80f83daa797f94c5c66"
        "aa46f3fe2a8363f5f64e7a021e30a1b8"
    )


def test_imported_catalog_keeps_existing_photos(
    imported_catalog: Catalog,
) -> None:
    photo_ids = [
        item.photo_file_id
        for item in _all_items(imported_catalog)
        if item.photo_file_id is not None
    ]

    assert photo_ids
    assert all(photo_id.strip() for photo_id in photo_ids)


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


def test_existing_imported_ids_remain_unchanged(
    imported_catalog: Catalog,
) -> None:
    category = imported_catalog.categories[0]
    assert category.id == "cocktails"
    assert {
        subcategory.id: [item.id for item in subcategory.items]
        for subcategory in category.subcategories
    } == {
        "signature": [
            "gin-tonic-yuzu",
            "kamanin-tigress",
            "pornstar-girl",
            "pornstar-boy",
            "pornstar-strawb",
            "cryptocurrency",
            "kiss-me",
            "kiwi-punch",
            "black-hive",
            "mango-sayer",
            "basil-smash",
            "butterfly",
            "allure",
            "huddart-smoky",
            "ultimo-turno",
        ],
        "classic": [
            "cosmopolitan",
            "whiskey-sour",
            "gin-sour",
            "old-fashioned",
            "negroni",
            "boulevardier",
            "margarita",
            "aperol-spritz",
        ],
        "non_alcoholic": ["berry-bloom-na"],
        "methods": [],
    }


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
