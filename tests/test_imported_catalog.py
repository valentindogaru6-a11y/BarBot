"""Integrity tests for the production catalog imported from bar XLS cards."""

import re
from pathlib import Path

import pytest

from app.keyboards.callbacks import ItemCallback
from app.models.catalog import Catalog
from app.storage.json_storage import JsonCatalogStorage
from source.import_bar_xls import (
    AUTHORITATIVE_RECIPE_IDS,
    CatalogImporter,
    OWNER_CURATED_LEMONADE_RECIPE_IDS,
)


LEGACY_STABLE_IDS = {
    "allure",
    "amerikano",
    "amerikano_s",
    "apelsinovyy_fresh",
    "aperol-spritz",
    "b_52",
    "basil-smash",
    "bazilikovaya",
    "berry-bloom-na",
    "black-hive",
    "boulevardier",
    "bumble_bee",
    "butterfly",
    "cosmopolitan",
    "cryptocurrency",
    "espresso",
    "flet_uayt",
    "flet_uayt_na",
    "gin-sour",
    "gin-tonic-yuzu",
    "gin_na",
    "gin_na_listyakh",
    "green_mexican",
    "greypfrut_fresh",
    "greypfrut_kordial",
    "hiroshima",
    "huddart-smoky",
    "imbirnyy_fresh",
    "kakao",
    "kakao_na",
    "kakao_s",
    "kakao_s_2",
    "kamanin-tigress",
    "kapuchino",
    "kapuchino_na",
    "kiss-me",
    "kiwi-punch",
    "klubnichnoe_pyure",
    "kokosovaya_pena",
    "latte",
    "latte_na",
    "laymovyy_fresh",
    "limonadnaya",
    "limonadnaya_osnova",
    "limonnyy_fresh",
    "malina_vinograd",
    "mango-sayer",
    "mango_marakuya_p_f",
    "margarita",
    "matcha_kapuchino",
    "matcha_na",
    "morkovnyy_fresh",
    "nastoyka_absolut",
    "negroni",
    "oblepikhovoe_pyure",
    "ogurechnyy_fresh",
    "old-fashioned",
    "osnova_iz",
    "polunichno",
    "pornstar-boy",
    "pornstar-girl",
    "pornstar-strawb",
    "pyure_kivi",
    "raf",
    "sakharnyy_sirop",
    "seldereevyy_fresh",
    "sirop_gibiskusa",
    "smes_spetsiy",
    "ultimo-turno",
    "vanilnyy_sirop",
    "whiskey-sour",
    "yablochnyy_fresh",
    "yagodnoe_pyure",
    "yagodnyy_mors",
    "yagodnyy_p_f",
}


@pytest.fixture(scope="module")
def imported_catalog() -> Catalog:
    path = Path(__file__).resolve().parents[1] / "data" / "catalog.json"
    return JsonCatalogStorage(path).load()


def _all_items(catalog: Catalog):
    for category in catalog.categories:
        yield from category.items
        for subcategory in category.subcategories:
            yield from subcategory.items


def _subcategory_counts(catalog: Catalog, category_id: str) -> dict[str, int]:
    category = next(
        category
        for category in catalog.categories
        if category.id == category_id
    )
    return {
        subcategory.id: len(subcategory.items)
        for subcategory in category.subcategories
    }


def test_imported_catalog_counts(imported_catalog: Catalog) -> None:
    assert len(imported_catalog.categories) == 10
    assert len(list(_all_items(imported_catalog))) == 513
    assert _subcategory_counts(imported_catalog, "cocktails") == {
        "signature": 15,
        "old_cocktails": 49,
        "classic": 8,
        "old_classics": 15,
        "non_alcoholic": 9,
        "shots": 5,
        "hot_cocktails": 8,
    }


def test_spreadsheet_categories_are_grouped_once(
    imported_catalog: Catalog,
) -> None:
    by_id = {category.id: category for category in imported_catalog.categories}

    assert len(by_id) == len(imported_catalog.categories)
    assert set(by_id) == {
        "cocktails",
        "new_lemonades",
        "signature_tea",
        "classic_tea",
        "coffee",
        "wine",
        "polufabrikaty_p_f",
        "bar_alcohol",
        "soft_drinks",
        "desserts",
    }
    assert len(by_id["signature_tea"].items) == 10
    assert [item.name for item in by_id["new_lemonades"].items] == [
        "Айс ти",
        "Классический лимонад",
        "Крем Сода",
        "Лимонад Черешня",
        "Маракуйя - огурец",
        "Руби-земляника",
    ]
    assert [
        item.fields["tech_card_number"]
        for item in by_id["new_lemonades"].items
    ] == ["31967", "31968", "31963", "31958", "31959", "31966"]
    ordered_ids = [
        category.id
        for category in sorted(
            imported_catalog.categories,
            key=lambda category: (
                category.sort_order,
                category.name.casefold(),
                category.id,
            ),
        )
    ]
    assert ordered_ids.index("cocktails") + 1 == ordered_ids.index(
        "new_lemonades"
    )
    assert ordered_ids.index("new_lemonades") + 1 == ordered_ids.index(
        "signature_tea"
    )
    assert [item.id for item in by_id["classic_tea"].items] == [
        "chay_grechaniy",
        "chay_klasichniy",
    ]
    assert len(by_id["desserts"].items) == 10
    assert _subcategory_counts(imported_catalog, "coffee") == {
        "coffee_classics": 2,
        "coffee_milk": 8,
        "cocoa_matcha": 6,
        "coffee_special": 6,
    }
    assert _subcategory_counts(imported_catalog, "wine") == {
        "wine_white": 35,
        "wine_rose": 8,
        "wine_sparkling": 25,
        "wine_red": 32,
    }
    assert _subcategory_counts(imported_catalog, "bar_alcohol") == {
        "whisky": 25,
        "vodka": 9,
        "gin_spirits": 10,
        "rum_tequila": 15,
        "liqueurs": 21,
        "brandy": 5,
        "infusions": 10,
        "beer_cider": 17,
    }
    assert _subcategory_counts(imported_catalog, "soft_drinks") == {
        "lemonades": 18,
        "fresh_servings": 12,
        "smoothies": 3,
        "packaged_drinks": 16,
        "homemade_drinks": 14,
    }
    assert _subcategory_counts(imported_catalog, "polufabrikaty_p_f") == {
        "purees_bases": 11,
        "fresh_juices": 11,
        "syrups_cordials": 12,
        "lemonade_bases": 14,
        "infusions_mixes": 24,
        "dessert_bases": 7,
    }


def test_owner_curated_lemonade_recipes_survive_reimport(
    imported_catalog: Catalog,
) -> None:
    expected_syrups = {
        "ays_ti": "Сироп в ассортименте (Белый чай)",
        "krem_soda": "Сироп в ассортименте (Маршмеллоу)",
        "limonad_chereshnya": "Сироп в ассортименте (Черешня)",
        "marakuyya_ogurets": "Сироп в ассортименте (Кукумбер)",
        "rubi_zemlyanika": "Сироп в ассортименте (Руби/Клубника)",
    }
    assert OWNER_CURATED_LEMONADE_RECIPE_IDS == set(expected_syrups)

    reimported = CatalogImporter(
        imported_catalog.model_copy(deep=True)
    ).import_all()
    by_id = {item.id: item for item in _all_items(reimported)}

    assert {
        item_id: by_id[item_id].ingredients[0].name
        for item_id in expected_syrups
    } == expected_syrups


def test_import_preserves_every_preexisting_stable_id(
    imported_catalog: Catalog,
) -> None:
    imported_ids = {item.id for item in _all_items(imported_catalog)}

    assert len(LEGACY_STABLE_IDS) == 75
    assert LEGACY_STABLE_IDS <= imported_ids


def test_new_xls_cocktails_are_separate_from_original_signature_cards(
    imported_catalog: Catalog,
) -> None:
    cocktails = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )
    by_subcategory = {
        subcategory.id: subcategory
        for subcategory in cocktails.subcategories
    }
    expected_signature_ids = {
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
    }
    signature_ids = {
        item.id for item in by_subcategory["signature"].items
    }
    old_cocktail_ids = {
        item.id for item in by_subcategory["old_cocktails"].items
    }

    assert signature_ids == expected_signature_ids
    assert len(old_cocktail_ids) == 49
    assert signature_ids.isdisjoint(old_cocktail_ids)


def test_new_xls_classics_are_separate_from_original_classic_cards(
    imported_catalog: Catalog,
) -> None:
    cocktails = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )
    by_subcategory = {
        subcategory.id: subcategory
        for subcategory in cocktails.subcategories
    }
    expected_classic_ids = {
        "cosmopolitan",
        "whiskey-sour",
        "gin-sour",
        "old-fashioned",
        "negroni",
        "boulevardier",
        "margarita",
        "aperol-spritz",
    }
    classic_ids = {item.id for item in by_subcategory["classic"].items}
    old_classic_ids = {
        item.id for item in by_subcategory["old_classics"].items
    }

    assert classic_ids == expected_classic_ids
    assert len(old_classic_ids) == 15
    assert classic_ids.isdisjoint(old_classic_ids)


def test_cocktail_sections_have_requested_order_without_methods(
    imported_catalog: Catalog,
) -> None:
    cocktails = next(
        category
        for category in imported_catalog.categories
        if category.id == "cocktails"
    )

    assert [subcategory.id for subcategory in cocktails.subcategories] == [
        "signature",
        "classic",
        "shots",
        "old_cocktails",
        "old_classics",
        "non_alcoholic",
        "hot_cocktails",
    ]


def test_signature_and_classic_teas_are_separate(
    imported_catalog: Catalog,
) -> None:
    by_id = {category.id: category for category in imported_catalog.categories}
    signature_ids = {item.id for item in by_id["signature_tea"].items}
    classic_ids = {item.id for item in by_id["classic_tea"].items}
    hot_cocktails = next(
        subcategory
        for subcategory in by_id["cocktails"].subcategories
        if subcategory.id == "hot_cocktails"
    )

    assert signature_ids.isdisjoint(classic_ids)
    assert classic_ids == {"chay_grechaniy", "chay_klasichniy"}
    assert {"glintveyn_b_a", "glintveyn_b_a_2"} <= {
        item.id for item in hot_cocktails.items
    }


def test_imported_catalog_keeps_existing_photos(
    imported_catalog: Catalog,
) -> None:
    photo_ids = [
        item.photo_file_id
        for item in _all_items(imported_catalog)
        if item.photo_file_id is not None
    ]

    assert len(photo_ids) >= 30
    assert all(photo_id and photo_id.strip() for photo_id in photo_ids)


def test_all_catalog_ids_are_globally_unique(
    imported_catalog: Catalog,
) -> None:
    ids = []
    for category in imported_catalog.categories:
        ids.append(category.id)
        ids.extend(item.id for item in category.items)
        for subcategory in category.subcategories:
            ids.append(subcategory.id)
            ids.extend(item.id for item in subcategory.items)

    assert len(ids) == len(set(ids))


def test_display_names_are_unique(imported_catalog: Catalog) -> None:
    names = [item.name.casefold() for item in _all_items(imported_catalog)]

    assert len(names) == len(set(names))


def test_demo_item_was_removed(imported_catalog: Catalog) -> None:
    items = list(_all_items(imported_catalog))

    assert "test-passion-cocktail" not in {item.id for item in items}
    assert all("тестов" not in item.name.casefold() for item in items)


def test_xls_import_preserves_only_preexisting_manual_steps(
    imported_catalog: Catalog,
) -> None:
    source_items = [
        item
        for item in _all_items(imported_catalog)
        if "tech_card_number" in item.fields
    ]
    items_with_steps = {item.id for item in source_items if item.steps}

    assert len(source_items) == 511
    assert items_with_steps == {
        "b_52",
        "green_mexican",
        "hiroshima",
        "mango_marakuya_p_f",
        "malina_vinograd",
    }


def test_every_catalog_card_has_ingredients(imported_catalog: Catalog) -> None:
    items = list(_all_items(imported_catalog))

    assert sum(len(item.ingredients) for item in items) == 1478
    assert all(item.ingredients for item in items)


def test_shot_cards_use_current_spreadsheet_recipes(
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

    assert (shots.name, shots.emoji, shots.sort_order) == ("Шоты", "🥃", 30)
    assert [item.name for item in shots.items] == [
        "B-52",
        "Green Mexican",
        "Hiroshima",
        "Golden Rain",
        "Meduza",
    ]
    assert {
        item.fields["tech_card_number"] for item in shots.items
    } == {"01372", "01373", "01374", "01375", "01376"}
    assert all(item.ingredients for item in shots.items)


def test_xls_cards_have_source_metadata_and_clean_units(
    imported_catalog: Catalog,
) -> None:
    source_items = [
        item
        for item in _all_items(imported_catalog)
        if "tech_card_number" in item.fields
    ]

    assert len(source_items) == 511
    assert all(item.fields["tech_card_number"] for item in source_items)
    assert all(item.fields["tech_card_date"] for item in source_items)
    assert all(item.fields["yield"] for item in source_items)
    assert all(item.ingredients for item in source_items)
    assert all(
        ingredient.unit not in {"кг", "л"}
        for item in source_items
        for ingredient in item.ingredients
    )
    assert all(
        re.search(r"\b(?:220[0-9]|210[0-9])\b", ingredient.name) is None
        for item in source_items
        for ingredient in item.ingredients
    )


def test_existing_cards_receive_latest_source_data(
    imported_catalog: Catalog,
) -> None:
    by_id = {item.id: item for item in _all_items(imported_catalog)}

    strawberry = by_id["klubnichnoe_pyure"]
    assert strawberry.fields == {
        "tech_card_number": "00594",
        "tech_card_date": "01.03.2025",
        "yield": "1500 г",
    }
    assert [
        (entry.name, entry.amount, entry.unit)
        for entry in strawberry.ingredients
    ] == [
        ("Ягоды с/м", 600, "г"),
        ("Сахар песок", 500, "г"),
        ("Вода", 400, "мл"),
    ]

    assert by_id["malina_vinograd"].fields == {
        "tech_card_number": "31822",
        "tech_card_date": "01.05.2026",
        "yield": "1075 г",
    }
    assert by_id["berry-bloom-na"].fields["tech_card_number"] == "31700"
    assert "berry_blum_n_a" not in by_id


def test_text_source_preserves_precise_cocktail_ingredients(
    imported_catalog: Catalog,
) -> None:
    assert "kiss-me" in AUTHORITATIVE_RECIPE_IDS

    reimported = CatalogImporter(
        imported_catalog.model_copy(deep=True)
    ).import_all()
    by_id = {item.id: item for item in _all_items(reimported)}

    assert [
        ingredient.name for ingredient in by_id["kiss-me"].ingredients
    ] == [
        "Виски Банкхолл",
        "Ликёр De Kyper (амаретто)",
        "Фреш лимона",
        "Сахарный сироп",
        "Monin сироп орех",
        "Monin сироп попкорн",
        "Easy Foam",
    ]
    assert [
        ingredient.name for ingredient in by_id["kiwi-punch"].ingredients
    ] == [
        "Бренди писко",
        "Monin сироп киви",
        "Ликёр De Kyper (зелёная дыня)",
        "Фреш лимона",
        "Easy Foam",
    ]
    assert [
        ingredient.name for ingredient in by_id["cosmopolitan"].ingredients
    ] == [
        "Водка Финляндия (клюква)",
        "Ликёр De Kyper (трипл-сек)",
        "Фреш лимона п/ф",
        "Сок гранатовый",
        "Лайм",
    ]


def test_text_source_cocktails_have_no_generic_xls_flavours(
    imported_catalog: Catalog,
) -> None:
    source_ids = {
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
        "berry-bloom-na",
        "cosmopolitan",
        "whiskey-sour",
        "gin-sour",
        "old-fashioned",
        "negroni",
        "boulevardier",
        "margarita",
        "aperol-spritz",
    }
    source_items = [
        item
        for item in _all_items(imported_catalog)
        if item.id in source_ids
    ]

    assert len(source_items) == 24
    assert all(
        "в ассортименте" not in ingredient.name.casefold()
        and "в асс" not in ingredient.name.casefold()
        for item in source_items
        for ingredient in item.ingredients
    )


def test_legacy_text_recipes_are_structured(imported_catalog: Catalog) -> None:
    by_id = {item.id: item for item in _all_items(imported_catalog)}

    assert len(by_id["yagodnyy_p_f"].ingredients) == 3
    assert len(by_id["polunichno"].ingredients) == 5


def test_all_imported_item_callbacks_fit_telegram_limit(
    imported_catalog: Catalog,
) -> None:
    callbacks = []
    for category in imported_catalog.categories:
        callbacks.extend(
            ItemCallback(category_id=category.id, item_id=item.id).pack()
            for item in category.items
        )
        callbacks.extend(
            ItemCallback(
                category_id=category.id,
                subcategory_id=subcategory.id,
                item_id=item.id,
            ).pack()
            for subcategory in category.subcategories
            for item in subcategory.items
        )

    assert callbacks
    assert max(len(callback.encode("utf-8")) for callback in callbacks) <= 64
