"""Tests for administrator configuration and authorization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.handlers.admin.common import require_admin_callback
from app.keyboards.main_menu import build_main_menu_keyboard
from app.services.auth_service import AuthService, is_admin


def _settings(admin_ids: object) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="test-token",
        admin_ids=admin_ids,
    )


def _button_texts(is_administrator: bool) -> list[str]:
    markup = build_main_menu_keyboard(is_admin=is_administrator)
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def test_parses_multiple_admin_ids() -> None:
    assert _settings("123456789, 987654321").admin_ids == frozenset(
        {123456789, 987654321}
    )


def test_empty_admin_ids() -> None:
    assert _settings("").admin_ids == frozenset()


@pytest.mark.parametrize("value", ["abc", "123,x", "0", "-7"])
def test_rejects_invalid_admin_ids(value: str) -> None:
    with pytest.raises(ValidationError, match="ADMIN_IDS"):
        _settings(value)


def test_is_admin_uses_only_user_id() -> None:
    service = AuthService(frozenset({10}))

    assert is_admin(10, {10})
    assert service.is_admin(10)
    assert not service.is_admin(11)


def test_user_menu_has_no_admin_button() -> None:
    assert "🛠 Управление каталогом" not in _button_texts(False)


def test_admin_menu_has_admin_button() -> None:
    assert "🛠 Управление каталогом" in _button_texts(True)


def test_direct_admin_callback_is_denied_for_user() -> None:
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=55),
        answer=AsyncMock(),
    )

    allowed = asyncio.run(
        require_admin_callback(
            callback,
            AuthService(frozenset({99})),
        )
    )

    assert not allowed
    callback.answer.assert_awaited_once_with(
        "Недостаточно прав",
        show_alert=True,
    )
