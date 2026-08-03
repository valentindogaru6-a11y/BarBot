"""Compact callback factories for user and role administration."""

from aiogram.filters.callback_data import CallbackData


class UserAdminCallback(CallbackData, prefix="um"):
    action: str


class UserListCallback(CallbackData, prefix="ul"):
    role: str
    page: int = 0


class UserViewCallback(CallbackData, prefix="uv"):
    user_id: int
    role: str = "all"
    page: int = 0


class UserRoleCallback(CallbackData, prefix="ur"):
    action: str
    user_id: int
    role: str
