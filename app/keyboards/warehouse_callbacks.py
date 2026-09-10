"""Compact callback namespace for warehouse Telegram UI."""

from aiogram.filters.callback_data import CallbackData


class WarehouseCallback(CallbackData, prefix="w"):
    action: str
    object_id: int = 0
    aux: int = 0
