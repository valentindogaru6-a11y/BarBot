"""Typed callback-data factories using only stable catalog identifiers."""

from typing import Literal

from aiogram.filters.callback_data import CallbackData


class MainCallback(CallbackData, prefix="m"):
    """Navigate between top-level bot sections."""

    action: Literal["home", "catalog", "search"]


class CategoryCallback(CallbackData, prefix="c"):
    """Open a category by stable ID."""

    category_id: str


class SubcategoryCallback(CallbackData, prefix="u"):
    """Open a subcategory by stable category and subcategory IDs."""

    category_id: str
    subcategory_id: str


class ItemCallback(CallbackData, prefix="i"):
    """Open an item by stable category, optional subcategory, and item IDs."""

    category_id: str
    item_id: str
    subcategory_id: str = "_"


class PaginationCallback(CallbackData, prefix="p"):
    """Move to a bounded page of categories or items."""

    section: Literal["categories", "subcategories", "items"]
    page: int
    category_id: str = "_"
    subcategory_id: str = "_"


class SearchCallback(CallbackData, prefix="s"):
    """Control the search conversation."""

    action: Literal["cancel", "retry", "voice_retry"]


class AssistantCandidateCallback(CallbackData, prefix="ai"):
    """Select one locally resolved AI catalog candidate by stable IDs."""

    category_id: str
    item_id: str
    subcategory_id: str = "_"
