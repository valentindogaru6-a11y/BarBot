"""Tests for opening the existing editor from a regular item card."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.admin import items as item_handlers
from app.keyboards.admin_callbacks import AdminItemCallback
from app.models.catalog import Catalog
from app.services.auth_service import AuthService


class _FakeMessage:
    pass


def _callback(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=_FakeMessage(),
        answer=AsyncMock(),
    )


def _editor_callback() -> AdminItemCallback:
    return AdminItemCallback(
        action="o",
        category_id="cocktails",
        subcategory_id="classic",
        item_id="negroni",
    )


def test_card_callback_opens_existing_item_editor(monkeypatch) -> None:
    callback = _callback(10)
    admin_service = SimpleNamespace()
    editor = AsyncMock(return_value=True)
    monkeypatch.setattr(item_handlers, "Message", _FakeMessage)
    monkeypatch.setattr(item_handlers, "show_item_editor", editor)

    asyncio.run(
        item_handlers.item_open(
            callback,
            _editor_callback(),
            AuthService(frozenset({10})),
            admin_service,
        )
    )

    callback.answer.assert_awaited_once_with()
    editor.assert_awaited_once_with(
        callback.message,
        admin_service,
        "cocktails",
        "classic",
        "negroni",
    )


def test_forged_card_callback_cannot_open_editor(monkeypatch) -> None:
    callback = _callback(11)
    editor = AsyncMock()
    monkeypatch.setattr(item_handlers, "Message", _FakeMessage)
    monkeypatch.setattr(item_handlers, "show_item_editor", editor)

    asyncio.run(
        item_handlers.item_open(
            callback,
            _editor_callback(),
            AuthService(frozenset({10})),
            SimpleNamespace(),
        )
    )

    callback.answer.assert_awaited_once_with(
        "Недостаточно прав",
        show_alert=True,
    )
    editor.assert_not_awaited()


def test_item_editor_helper_renders_existing_edit_menu(monkeypatch) -> None:
    rendered = AsyncMock()
    service = SimpleNamespace(
        get_catalog=AsyncMock(
            return_value=Catalog.model_validate(
                {
                    "version": 1,
                    "categories": [
                        {
                            "id": "cocktails",
                            "name": "Коктейли",
                            "subcategories": [
                                {
                                    "id": "classic",
                                    "name": "Классические",
                                    "items": [
                                        {
                                            "id": "negroni",
                                            "name": "Negroni",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
        )
    )
    monkeypatch.setattr(item_handlers, "edit_or_send_text", rendered)

    shown = asyncio.run(
        item_handlers.show_item_editor(
            _FakeMessage(),
            service,
            "cocktails",
            "classic",
            "negroni",
        )
    )

    assert shown
    markup = rendered.await_args.args[2]
    callbacks = [
        AdminItemCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
        and button.callback_data.startswith("at:")
    ]
    edit_name = next(
        callback for callback in callbacks if callback.action == "en"
    )
    assert edit_name.category_id == "cocktails"
    assert edit_name.subcategory_id == "classic"
    assert edit_name.item_id == "negroni"


def test_missing_or_moved_item_has_safe_navigation(monkeypatch) -> None:
    rendered = AsyncMock()
    service = SimpleNamespace(
        get_catalog=AsyncMock(
            return_value=Catalog(
                version=1,
                categories=[],
            )
        )
    )
    monkeypatch.setattr(item_handlers, "edit_or_send_text", rendered)

    shown = asyncio.run(
        item_handlers.show_item_editor(
            _FakeMessage(),
            service,
            "cocktails",
            "classic",
            "negroni",
        )
    )

    assert not shown
    rendered.assert_awaited_once()
    text = rendered.await_args.args[1]
    markup = rendered.await_args.args[2]
    button_texts = {
        button.text
        for row in markup.inline_keyboard
        for button in row
    }
    assert "удалена или перемещена" in text
    assert button_texts == {
        "📚 Вернуться в каталог",
        "🛠 Админ-панель",
    }
