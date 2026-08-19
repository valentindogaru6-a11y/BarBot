"""Unit tests for lightweight universal phonetic aliases."""

import pytest

from app.models.catalog_item import CatalogItem
from app.services.voice_alias_service import (
    MAX_AUTOMATIC_ALIASES,
    VoiceAliasService,
    contains_cyrillic,
    contains_latin,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Kiwi Punch", "киви панч"),
        ("Pornstar Girl", "порнстар герл"),
        ("Black Hive", "блэк хайв"),
        ("Old Fashioned", "олд фэшн"),
        ("Whiskey Sour", "виски сауэр"),
        ("Aperol Spritz", "апероль спритц"),
        ("Boulevardier", "булевардье"),
        ("Butterfly", "баттерфляй"),
        ("Allure", "аллюр"),
    ],
)
def test_known_names_have_expected_phonetic_alias(
    name: str,
    expected: str,
) -> None:
    item = CatalogItem(id="item", name=name)

    assert expected in VoiceAliasService().build_aliases(item)


def test_manual_aliases_are_normalized_and_deduplicated() -> None:
    item = CatalogItem(
        id="kiwi-punch",
        name="Kiwi Punch",
        voice_aliases=[
            "  КИВИ   ПАНЧ  ",
            "киви-панч",
            "Киви, панч!",
            "авторский вариант",
        ],
    )

    aliases = VoiceAliasService().build_aliases(item)

    assert "авторский вариант" in aliases
    assert "киви панч" in aliases
    assert sum(alias == "киви панч" for alias in aliases) == 1


def test_punctuation_and_unicode_forms_are_normalized() -> None:
    item = CatalogItem(
        id="black-hive",
        name="Ｂｌａｃｋ—Hive!!!",
        voice_aliases=["БЛЭК_ХАЙВ"],
    )

    aliases = VoiceAliasService().build_aliases(item)

    assert "black hive" in aliases
    assert "блэк хайв" in aliases
    assert all("—" not in alias and "_" not in alias for alias in aliases)


def test_generic_rules_produce_bounded_cyrillic_variants() -> None:
    item = CatalogItem(id="future-item", name="Cheeky Queen Flow")

    aliases = VoiceAliasService().build_aliases(item)
    automatic = aliases - {"cheeky queen flow"}

    assert automatic
    assert len(automatic) <= MAX_AUTOMATIC_ALIASES
    assert all(contains_cyrillic(alias) for alias in automatic)
    assert all(not contains_latin(alias) for alias in automatic)


def test_alias_generation_does_not_mutate_item() -> None:
    item = CatalogItem(
        id="manual",
        name="Flat White",
        voice_aliases=["флэт уайт"],
        photo_file_id="telegram-photo-id",
    )
    original = item.model_copy(deep=True)

    first = VoiceAliasService().build_aliases(item)
    second = VoiceAliasService().build_aliases(item)

    assert first == second
    assert item == original
