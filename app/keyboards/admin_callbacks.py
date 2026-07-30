"""Compact callback-data factories for the administrator interface."""

from aiogram.filters.callback_data import CallbackData


class AdminPanelCallback(CallbackData, prefix="ad"):
    action: str


class AdminEntityCallback(CallbackData, prefix="ae"):
    entity: str


class AdminCategoryCallback(CallbackData, prefix="ag"):
    action: str
    category_id: str = "_"


class AdminSubcategoryCallback(CallbackData, prefix="au"):
    action: str
    category_id: str
    subcategory_id: str = "_"


class AdminItemCallback(CallbackData, prefix="at"):
    action: str
    category_id: str
    item_id: str = "_"
    subcategory_id: str = "_"


class AdminConfirmCallback(CallbackData, prefix="ay"):
    action: str
    category_id: str = "_"
    item_id: str = "_"
    subcategory_id: str = "_"


class AdminCancelCallback(CallbackData, prefix="az"):
    action: str = "x"


class AdminPaginationCallback(CallbackData, prefix="ap"):
    entity: str
    filter_mode: str
    page: int
    category_id: str = "_"
    subcategory_id: str = "_"
