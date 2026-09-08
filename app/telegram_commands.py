"""Role-aware registration of official Telegram Bot Commands."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.models.user import Role
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

USER_COMMANDS = (
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="catalog", description="Открыть каталог"),
    BotCommand(command="search", description="Поиск по каталогу"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="myid", description="Показать мой Telegram ID"),
)
ADMIN_COMMANDS = USER_COMMANDS[:3] + (
    BotCommand(command="admin", description="Управление ботом"),
    BotCommand(command="users", description="Управление пользователями"),
) + USER_COMMANDS[3:]
ACCOUNTANT_COMMANDS = (
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="myid", description="Показать мой Telegram ID"),
)
BLOCKED_COMMANDS = (
    BotCommand(command="start", description="Проверить доступ"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="myid", description="Показать мой Telegram ID"),
)


def commands_for_role(role: Role) -> tuple[BotCommand, ...]:
    if role in {Role.SUPER_ADMIN, Role.ADMIN}:
        return ADMIN_COMMANDS
    if role is Role.ACCOUNTANT:
        return ACCOUNTANT_COMMANDS
    if role is Role.BLOCKED:
        return BLOCKED_COMMANDS
    return USER_COMMANDS


async def update_user_commands(
    bot: Bot,
    telegram_user_id: int,
    role: Role,
) -> bool:
    """Update one private chat scope without exposing its ID in logs."""

    try:
        await bot.set_my_commands(
            commands=list(commands_for_role(role)),
            scope=BotCommandScopeChat(chat_id=telegram_user_id),
        )
        return True
    except TelegramAPIError:
        logger.exception("Could not update role-specific Telegram commands")
        return False


async def configure_bot_commands(bot: Bot, auth_service: AuthService) -> None:
    """Register defaults and scopes for known privileged/blocked users."""

    try:
        await bot.set_my_commands(
            commands=list(USER_COMMANDS),
            scope=BotCommandScopeDefault(),
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError:
        logger.exception("Could not configure default Telegram commands")

    scoped: dict[int, Role] = {
        user_id: Role.SUPER_ADMIN
        for user_id in auth_service.super_admin_ids
    }
    if auth_service.user_service is not None:
        for role in (Role.ADMIN, Role.ACCOUNTANT, Role.BLOCKED):
            users, _, _ = auth_service.user_service.list_users(
                role=role,
                page_size=100_000,
            )
            for user in users:
                scoped.setdefault(user.telegram_user_id, role)
    for user_id, role in scoped.items():
        await update_user_commands(bot, user_id, role)
