"""Tests for compact administrator callback data."""

from app.keyboards.admin_callbacks import (
    AdminCancelCallback,
    AdminCategoryCallback,
    AdminConfirmCallback,
    AdminEntityCallback,
    AdminItemCallback,
    AdminPaginationCallback,
    AdminPanelCallback,
    AdminSubcategoryCallback,
)


def test_all_admin_callbacks_fit_telegram_limit() -> None:
    callbacks = [
        AdminPanelCallback(action="o"),
        AdminEntityCallback(entity="c"),
        AdminCategoryCallback(action="en", category_id="a" * 18),
        AdminSubcategoryCallback(
            action="en",
            category_id="a" * 18,
            subcategory_id="b" * 18,
        ),
        AdminItemCallback(
            action="en",
            category_id="a" * 18,
            subcategory_id="b" * 18,
            item_id="c" * 18,
        ),
        AdminConfirmCallback(
            action="di",
            category_id="a" * 18,
            subcategory_id="b" * 18,
            item_id="c" * 18,
        ),
        AdminCancelCallback(),
        AdminPaginationCallback(
            entity="i",
            filter_mode="h",
            page=999,
            category_id="a" * 18,
            subcategory_id="b" * 18,
        ),
    ]

    assert all(len(callback.pack().encode()) <= 64 for callback in callbacks)
