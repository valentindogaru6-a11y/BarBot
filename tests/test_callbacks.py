"""Tests for typed and size-safe callback data."""

from app.keyboards.callbacks import (
    CategoryCallback,
    ItemCallback,
    MainCallback,
    PaginationCallback,
    SearchCallback,
)


def test_callback_factories_pack_and_unpack_stable_ids() -> None:
    packed = ItemCallback(
        category_id="cocktails",
        item_id="passion-sour",
    ).pack()

    unpacked = ItemCallback.unpack(packed)

    assert unpacked.category_id == "cocktails"
    assert unpacked.item_id == "passion-sour"
    assert "Маракуйя Сауэр" not in packed


def test_all_callback_factories_produce_short_data() -> None:
    callbacks = [
        MainCallback(action="catalog"),
        CategoryCallback(category_id="a" * 28),
        ItemCallback(category_id="a" * 28, item_id="b" * 28),
        PaginationCallback(
            section="items",
            page=999,
            category_id="a" * 28,
        ),
        SearchCallback(action="retry"),
    ]

    assert all(len(callback.pack().encode("utf-8")) <= 64 for callback in callbacks)
