"""Tests for Telegram command registration and command-menu scopes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.bot import ADMIN_COMMANDS, USER_COMMANDS, configure_bot_commands


def _names(commands) -> list[str]:
    return [command.command for command in commands]


def test_user_and_admin_command_sets() -> None:
    assert _names(USER_COMMANDS) == [
        "start",
        "catalog",
        "search",
        "help",
    ]
    assert _names(ADMIN_COMMANDS) == [
        "start",
        "catalog",
        "search",
        "help",
        "admin",
        "myid",
    ]


def test_configures_default_and_per_admin_command_scopes() -> None:
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
    )

    asyncio.run(configure_bot_commands(bot, frozenset({20, 10})))

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
