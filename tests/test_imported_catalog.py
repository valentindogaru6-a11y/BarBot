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
    assert len(imported_catalog.categories[0].subcategories) == 5
    assert len(list(_all_items(imported_catalog))) == 77
    counts = {
        subcategory.id: len(subcategory.items)
        for subcategory in imported_catalog.categories[0].subcategories
    }
    assert counts == {
        "signature": 15,
        "classic": 8,
        "non_alcoholic": 1,
        "methods": 0,
        "shots": 3,
    }


def test_spreadsheet_categories_are_grouped_once(
    imported_catalog: Catalog,
) -> None:
    by_id = {category.id: category for category in imported_catalog.categories}

    assert len(by_id) == len(imported_catalog.categories)
    assert (by_id["coffee"].name, by_id["coffee"].emoji) == (
        "Кофейная карта",
        "☕",
    )
    assert {
        subcategory.id: len(subcategory.items)
        for subcategory in by_id["coffee"].subcategories
    } == {
        "coffee_classics": 2,
        "coffee_milk": 8,
        "cocoa_matcha": 6,
        "coffee_special": 1,
    }
    assert (
        by_id["polufabrikaty_p_f"].name,
        by_id["polufabrikaty_p_f"].emoji,
    ) == ("Полуфабрикаты", "🧪")
    assert {
        subcategory.id: len(subcategory.items)
        for subcategory in by_id["polufabrikaty_p_f"].subcategories
    } == {
        "purees_bases": 9,
        "fresh_juices": 9,
        "syrups_cordials": 4,
        "lemonade_bases": 6,
        "infusions_mixes": 5,
    }
    for category_id in ("signature_tea", "wine"):
        assert by_id[category_id].items == []
        assert by_id[category_id].subcategories == []


def test_existing_cocktail_data_is_semantically_unchanged_before_shots(
    imported_catalog: Catalog,
) -> None:
    cocktail = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )
    cocktail_data = cocktail.model_dump(mode="json")
    cocktail_data["subcategories"] = [
        subcategory
        for subcategory in cocktail_data["subcategories"]
        if subcategory["id"] != "shots"
    ]
    payload = json.dumps(
        cocktail_data,
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
        "shots": ["b_52", "green_mexican", "hiroshima"],
    }


def test_demo_item_was_removed(imported_catalog: Catalog) -> None:
    item_ids = {item.id for item in _all_items(imported_catalog)}

    assert "test-passion-cocktail" not in item_ids
    assert all("тестов" not in item.name.casefold() for item in _all_items(imported_catalog))


def test_xls_import_preserves_manual_steps_without_inventing_new_ones(
    imported_catalog: Catalog,
) -> None:
    xls_items = [
        item
        for item in _all_items(imported_catalog)
        if "tech_card_number" in item.fields
    ]
    items_with_steps = [item.id for item in xls_items if item.steps]

    assert len(xls_items) == 46
    assert items_with_steps == ["mango_marakuya_p_f"]


def test_imported_ingredient_count(imported_catalog: Catalog) -> None:
    assert sum(
        len(item.ingredients)
        for item in _all_items(imported_catalog)
    ) == 249


def test_shot_cards_have_complete_draft_recipes(
    imported_catalog: Catalog,
) -> None:
    cocktails = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )
    shots = next(
        subcategory
        for subcategory in cocktails.subcategories
        if subcategory.id == "shots"
    )

    assert (shots.name, shots.emoji, shots.sort_order) == ("Шоты", "🥃", 50)
    assert [item.name for item in shots.items] == [
        "B-52",
        "Green Mexican",
        "Hiroshima",
    ]
    assert all(item.ingredients for item in shots.items)
    assert all(item.steps for item in shots.items)
    assert all(item.fields["method"] == "Layer" for item in shots.items)
    assert all(item.photo_file_id is None for item in shots.items)


def test_xls_cards_have_source_metadata_and_ingredients(
    imported_catalog: Catalog,
) -> None:
    xls_items = [
        item
        for item in _all_items(imported_catalog)
        if "tech_card_number" in item.fields
    ]

    assert len(xls_items) == 46
    assert all(item.fields["tech_card_number"] for item in xls_items)
    assert all(item.fields["tech_card_date"] for item in xls_items)
    assert all(item.ingredients for item in xls_items)

    strawberry = next(
        item for item in xls_items if item.name == "Клубничное пюре"
    )
    assert strawberry.fields == {
        "tech_card_number": "00594",
        "tech_card_date": "01.03.2025",
        "yield": "1500 г",
    }
    assert [(entry.name, entry.amount, entry.unit) for entry in strawberry.ingredients] == [
        ("Ягоды с/м", 600, "г"),
        ("Сахар", 500, "г"),
        ("Вода", 400, "мл"),
    ]

    corrected_units = {
        item.fields["tech_card_number"]: item.fields["yield"]
        for item in xls_items
        if item.fields["tech_card_number"] in {"00611", "02154", "30246"}
    }
    assert corrected_units == {
        "00611": "700 мл",
        "02154": "1000 мл",
        "30246": "700 мл",
    }


def test_existing_semifinished_ids_are_preserved(
    imported_catalog: Catalog,
) -> None:
    category = next(
        category
        for category in imported_catalog.categories
        if category.id == "polufabrikaty_p_f"
    )
    ids = {
        item.id
        for subcategory in category.subcategories
        for item in subcategory.items
    }

    assert {
        "mango_marakuya_p_f",
        "kivi_ogurets_p_f",
        "malina_vinograd",
        "yagodnyy_p_f",
        "polunichno",
    } <= ids


def test_all_imported_item_callbacks_fit_telegram_limit(
    imported_catalog: Catalog,
) -> None:
    callbacks = [
        ItemCallback(
            category_id=category.id,
            subcategory_id=subcategory.id,
            item_id=item.id,
        ).pack()
        for category in imported_catalog.categories
        for subcategory in category.subcategories
        for item in subcategory.items
    ]

    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
