"""Tests for Telegram command registration and command-menu scopes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.models.user import Role
from app.services.auth_service import AuthService
from app.telegram_commands import (
    ADMIN_COMMANDS,
    BLOCKED_COMMANDS,
    USER_COMMANDS,
    commands_for_role,
    configure_bot_commands,
    update_user_commands,
)


def _names(commands) -> list[str]:
    return [command.command for command in commands]


def test_user_and_admin_command_sets() -> None:
    assert _names(USER_COMMANDS) == [
        "start",
        "catalog",
        "search",
        "help",
        "myid",
    ]
    assert _names(ADMIN_COMMANDS) == [
        "start",
        "catalog",
        "search",
        "help",
        "myid",
        "admin",
        "users",
    ]
    assert _names(BLOCKED_COMMANDS) == ["start", "help", "myid"]
    assert commands_for_role(Role.BARTENDER) == USER_COMMANDS
    assert commands_for_role(Role.BLOCKED) == BLOCKED_COMMANDS


def test_configures_default_and_per_admin_command_scopes() -> None:
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
    )

    asyncio.run(
        configure_bot_commands(
            bot,
            AuthService(frozenset({20, 10})),
        )
    )

    calls = bot.set_my_commands.await_args_list
    assert len(calls) == 3
    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    assert _names(calls[0].kwargs["commands"]) == _names(USER_COMMANDS)

    admin_scopes = [call.kwargs["scope"] for call in calls[1:]]
    assert all(isinstance(scope, BotCommandScopeChat) for scope in admin_scopes)
    assert [scope.chat_id for scope in admin_scopes] == [10, 20]
    assert all(
        _names(call.kwargs["commands"]) == _names(ADMIN_COMMANDS)
        for call in calls[1:]
    )
    menu_button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert isinstance(menu_button, MenuButtonCommands)


def test_updates_one_chat_scope_after_role_change() -> None:
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    result = asyncio.run(update_user_commands(bot, 55, Role.BLOCKED))

    assert result
    call = bot.set_my_commands.await_args
    assert _names(call.kwargs["commands"]) == _names(BLOCKED_COMMANDS)
    assert isinstance(call.kwargs["scope"], BotCommandScopeChat)
    assert call.kwargs["scope"].chat_id == 55
