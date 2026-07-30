"""Typed callback-data factories using only stable catalog identifiers."""

from typing import Literal

from aiogram.filters.callback_data import CallbackData


class MainCallback(CallbackData, prefix="m"):
    """Navigate between top-level bot sections."""

    action: Literal["home", "catalog", "search"]


class CategoryCallback(CallbackData, prefix="c"):
    """Open a category by stable ID."""

    category_id: str


class ItemCallback(CallbackData, prefix="i"):
    """Open an item by stable category and item IDs."""

    category_id: str
    item_id: str


class PaginationCallback(CallbackData, prefix="p"):
    """Move to a bounded page of categories or items."""

    section: Literal["categories", "items"]
    page: int
    category_id: str = "_"


class SearchCallback(CallbackData, prefix="s"):
    """Control the search conversation."""

    action: Literal["cancel", "retry"]
