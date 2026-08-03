"""Tests for user picker, role callbacks, and role-aware UI."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import SharedUser, User, UsersShared

from app.handlers.admin.user_states import RoleChangeStates
from app.handlers.admin import users as user_handlers
from app.handlers.admin.users import picker_users_shared
from app.handlers.start import handle_myid, handle_start
from app.middlewares.access import BLOCKED_TEXT
from app.keyboards.user_admin import (
    build_picker_reply_keyboard,
    build_user_card_keyboard,
    build_user_management_keyboard,
)
from app.keyboards.user_callbacks import (
    UserAdminCallback,
    UserListCallback,
    UserRoleCallback,
    UserViewCallback,
)
from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.storage.user_database import UserDatabase


class _State:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.current = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def get_state(self):
        return self.current

    async def set_state(self, state) -> None:
        self.current = state

    async def clear(self) -> None:
        self.data = {}
        self.current = None


def _services(tmp_path: Path) -> tuple[AuthService, UserService]:
    database = UserDatabase(tmp_path / "barbot.db")
    database.initialize()
    users = UserService(database)
    access = AuthService(frozenset({1}), users)
    users.ensure_user(1)
    return access, users


def test_picker_uses_single_non_bot_request() -> None:
    markup = build_picker_reply_keyboard(987)
    request = markup.keyboard[0][0].request_users

    assert request is not None
    assert request.request_id == 987
    assert request.user_is_bot is False
    assert request.max_quantity == 1
    assert request.request_name is True
    assert request.request_username is True
    assert markup.one_time_keyboard is True
    assert markup.is_persistent is False


def test_all_user_callbacks_fit_telegram_limit() -> None:
    callbacks = [
        UserAdminCallback(action="search"),
        UserListCallback(role="bartender", page=999),
        UserViewCallback(user_id=9_223_372_036_854_775_807, role="blocked", page=999),
        UserRoleCallback(
            action="confirm",
            user_id=9_223_372_036_854_775_807,
            role="bartender",
        ),
    ]

    assert all(len(callback.pack().encode()) <= 64 for callback in callbacks)


def test_admin_menu_hides_add_admin() -> None:
    regular = build_user_management_keyboard(can_assign_admin=False)
    super_admin = build_user_management_keyboard(can_assign_admin=True)
    regular_text = {button.text for row in regular.inline_keyboard for button in row}
    super_text = {button.text for row in super_admin.inline_keyboard for button in row}

    assert "➕ Добавить администратора" not in regular_text
    assert "➕ Добавить администратора" in super_text


def test_user_card_hides_current_and_forbidden_actions() -> None:
    markup = build_user_card_keyboard(
        target_id=50,
        current_role=Role.BARTENDER,
        allowed_roles=[Role.USER, Role.BLOCKED],
        back_role="bartender",
        back_page=0,
    )
    texts = {button.text for row in markup.inline_keyboard for button in row}

    assert "🍸 Назначить Bartender" not in texts
    assert "🛠 Назначить Admin" not in texts
    assert "👤 Сделать User" in texts
    assert "🚫 Заблокировать" in texts


def test_users_shared_shows_confirmation_before_role_write(
    tmp_path: Path,
) -> None:
    access, users = _services(tmp_path)
    state = _State({"request_id": 77, "new_role": Role.ADMIN.value})
    message = SimpleNamespace(
        from_user=User(id=1, is_bot=False, first_name="Owner"),
        users_shared=UsersShared(
            request_id=77,
            users=[
                SharedUser(
                    user_id=55,
                    first_name="Новый",
                    username="new_user",
                )
            ],
        ),
        answer=AsyncMock(),
    )

    asyncio.run(
        picker_users_shared(message, state, access, users)
    )

    assert users.get_user(55) is not None
    assert users.get_user(55).role is Role.USER
    assert state.current == RoleChangeStates.confirm
    assert message.answer.await_count == 2
    remove_markup = message.answer.await_args_list[0].kwargs["reply_markup"]
    assert remove_markup.remove_keyboard is True


def test_confirmed_role_updates_commands_and_failed_update_does_not_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    access, users = _services(tmp_path)
    users.ensure_user(56)
    callback = SimpleNamespace(
        from_user=User(id=1, is_bot=False, first_name="Owner"),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    state = _State({})
    command_update = AsyncMock(return_value=False)
    monkeypatch.setattr(user_handlers, "update_user_commands", command_update)

    asyncio.run(
        user_handlers.role_change_confirm(
            callback,
            UserRoleCallback(
                action="confirm",
                user_id=56,
                role=Role.ADMIN.value,
            ),
            state,
            access,
            users,
            SimpleNamespace(),
        )
    )

    assert users.get_user(56).role is Role.ADMIN
    command_update.assert_awaited_once()


def test_forged_user_management_callback_is_denied(tmp_path: Path) -> None:
    access, users = _services(tmp_path)
    users.ensure_user(70)
    users.ensure_user(71)
    callback = SimpleNamespace(
        from_user=User(id=70, is_bot=False, first_name="User"),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )

    asyncio.run(
        user_handlers.user_view(
            callback,
            UserViewCallback(user_id=71),
            access,
            users,
        )
    )

    callback.answer.assert_awaited_once_with(
        "Недостаточно прав",
        show_alert=True,
    )


def test_blocked_user_can_still_use_myid(tmp_path: Path) -> None:
    access, users = _services(tmp_path)
    users.ensure_user(40)
    users.set_role(40, Role.BLOCKED, actor_user_id=1)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=40),
        answer=AsyncMock(),
    )

    asyncio.run(handle_myid(message))

    message.answer.assert_awaited_once()
    assert not access.can_view_catalog(40)


def test_blocked_start_only_shows_restriction(tmp_path: Path) -> None:
    access, users = _services(tmp_path)
    users.ensure_user(41)
    users.set_role(41, Role.BLOCKED, actor_user_id=1)
    message = SimpleNamespace(
        from_user=User(id=41, is_bot=False, first_name="Blocked"),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    asyncio.run(handle_start(message, _State({}), access, bot))

    message.answer.assert_awaited_once_with(BLOCKED_TEXT)
