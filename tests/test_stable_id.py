"""Tests for deterministic stable-ID generation."""

import re

import pytest

from app.keyboards.admin_callbacks import AdminItemCallback
from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.services.stable_id import (
    StableIdGenerationError,
    generate_stable_id,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Джин-тоник юдзу", "gin_tonik_yuzu"),
        ("Їжак і ґанок", "yizhak_i_ganok"),
        ("Old Fashioned", "old_fashioned"),
        ("Pornstar Girl", "pornstar_girl"),
        ("Безалкогольные коктейли", "bezalkogolnye"),
    ],
)
def test_generates_transliterated_ids(name: str, expected: str) -> None:
    assert generate_stable_id(name, set(), MAX_STABLE_ID_LENGTH) == expected


def test_full_transliteration_is_available_before_production_truncation() -> None:
    assert (
        generate_stable_id("Каманинская тигрица", set(), 64)
        == "kamaninskaya_tigritsa"
    )


def test_normalizes_special_characters_and_repeated_separators() -> None:
    generated = generate_stable_id(
        "  Old---Fashioned___&& Soda!!! ",
        set(),
        MAX_STABLE_ID_LENGTH,
    )

    assert generated == "old_fashioned_soda"
    assert "__" not in generated


def test_generated_id_always_starts_with_a_letter() -> None:
    generated = generate_stable_id(
        "123 коктейль",
        set(),
        MAX_STABLE_ID_LENGTH,
    )

    assert generated == "id_123_kokteyl"
    assert generated[0].isalpha()


def test_generated_id_respects_length_and_alphabet() -> None:
    generated = generate_stable_id(
        "Supercalifragilisticexpialidocious",
        set(),
        MAX_STABLE_ID_LENGTH,
    )

    assert len(generated) == MAX_STABLE_ID_LENGTH
    assert re.fullmatch(r"[a-z][a-z0-9_]*", generated)


def test_adds_second_and_third_suffixes() -> None:
    assert generate_stable_id("Negroni", {"negroni"}, 18) == "negroni_2"
    assert (
        generate_stable_id("Negroni", {"negroni", "negroni_2"}, 18)
        == "negroni_3"
    )


def test_conflict_after_truncation_keeps_suffix_inside_limit() -> None:
    generated = generate_stable_id(
        "Supercalifragilistic",
        {"supercalif", "supercal_2"},
        10,
    )

    assert generated == "supercal_3"
    assert len(generated) == 10


def test_empty_transliteration_is_rejected() -> None:
    with pytest.raises(StableIdGenerationError, match="другое название"):
        generate_stable_id("🔥✨", set(), MAX_STABLE_ID_LENGTH)


def test_generation_is_deterministic() -> None:
    first = generate_stable_id("Маракуйя сауэр", {"marakuyya_sauer"}, 18)
    second = generate_stable_id("Маракуйя сауэр", {"marakuyya_sauer"}, 18)

    assert first == second


def test_generated_parent_ids_fit_item_callback_limit() -> None:
    category_id = generate_stable_id("A" * 40, set(), 18)
    subcategory_id = generate_stable_id("B" * 40, set(), 18)
    item_id = generate_stable_id("C" * 40, set(), 18)
    callback = AdminItemCallback(
        action="o",
        category_id=category_id,
        subcategory_id=subcategory_id,
        item_id=item_id,
    ).pack()

    assert len(callback.encode()) <= 64
