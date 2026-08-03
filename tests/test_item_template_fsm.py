"""Focused tests for full-card admin FSM orchestration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.admin import common as common_handlers
from app.handlers.admin import items as item_handlers
from app.handlers.admin.states import (
    EditItemStates,
    TemplateConfirmationStates,
    TemplateCorrectionStates,
    TemplateCreateItemStates,
)
from app.keyboards.admin_callbacks import AdminItemCallback
from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem, Ingredient
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.auth_service import AuthService
from app.services.card_template import serialize_item_template


class _State:
    def __init__(self, data: dict | None = None, current: str | None = None) -> None:
        self.data = data or {}
        self.current = current
        self.cleared = False

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_data(self, data: dict) -> None:
        self.data = dict(data)

    async def update_data(self, **changes) -> None:
        self.data.update(changes)

    async def set_state(self, state) -> None:
        self.current = getattr(state, "state", state)

    async def get_state(self) -> str | None:
        return self.current

    async def clear(self) -> None:
        self.data = {}
        self.current = None
        self.cleared = True


class _Message:
    def __init__(self, text: str | None = None, user_id: int = 10) -> None:
        self.text = text
        self.photo = None
        self.from_user = SimpleNamespace(
            id=user_id,
            username="admin",
            is_bot=False,
        )
        self.answer = AsyncMock()
        self.edit_text = AsyncMock()


def _callback(action: str, message: _Message, user_id: int = 10):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="admin"),
        message=message,
        answer=AsyncMock(),
    ), AdminItemCallback(
        action=action,
        category_id="cocktails",
        subcategory_id="classic",
        item_id="kiss_me",
    )


def _catalog(item: CatalogItem | None = None) -> Catalog:
    return Catalog(
        version=1,
        categories=[
            Category(
                id="cocktails",
                name="Коктейли",
                subcategories=[
                    Subcategory(
                        id="classic",
                        name="Классика",
                        items=[item] if item else [],
                    )
                ],
            )
        ],
    )


def _item(name: str = "Kiss Me") -> CatalogItem:
    return CatalogItem(
        id="kiss_me",
        name=name,
        photo_file_id="protected-photo",
        ingredients=[Ingredient(name="Виски", amount=45, unit="мл")],
        fields={"glassware": "Рокс"},
        steps=["Перебить"],
        tags=["виски"],
        sort_order=10,
    )


def test_long_copyable_text_is_split_without_truncation() -> None:
    text = "а" * 4090 + "\n" + "б" * 400

    chunks = item_handlers._split_telegram_text(text)

    assert len(chunks) == 2
    assert "".join(chunks) == text
    assert all(len(chunk) <= item_handlers.TELEGRAM_TEXT_LIMIT for chunk in chunks)


def test_individual_edit_shows_current_parser_text_and_force_reply(
    monkeypatch,
) -> None:
    message = _Message()
    callback, callback_data = _callback("en", message)
    state = _State()
    service = SimpleNamespace(get_catalog=AsyncMock(return_value=_catalog(_item())))
    monkeypatch.setattr(item_handlers, "Message", _Message)
    monkeypatch.setattr(item_handlers, "edit_or_send_text", AsyncMock())

    asyncio.run(
        item_handlers.item_edit_start(
            callback,
            callback_data,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    assert state.current == EditItemStates.value.state
    raw_call = message.answer.await_args_list[0]
    assert raw_call.args[0] == "Kiss Me"
    copy_button = raw_call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert copy_button.copy_text.text == "Kiss Me"
    force_reply = message.answer.await_args_list[-1].kwargs["reply_markup"]
    assert force_reply.force_reply is True
    assert len(force_reply.input_field_placeholder) < 64


def test_invalid_template_keeps_text_and_never_saves() -> None:
    template = serialize_item_template(_item()).replace(
        "ПОРЯДОК:\n10", "ПОРЯДОК:\nне число"
    )
    message = _Message(template)
    state = _State(
        {
            "template_mode": "edit",
            "category_id": "cocktails",
            "subcategory_id": "classic",
            "item_id": "kiss_me",
        }
    )
    service = SimpleNamespace(
        get_catalog=AsyncMock(return_value=_catalog(_item())),
        update_item=AsyncMock(),
    )

    asyncio.run(
        item_handlers.item_template_receive(
            message,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    assert state.current == TemplateCorrectionStates.input.state
    assert state.data["raw_template"] == template
    service.update_item.assert_not_awaited()
    error_text = message.answer.await_args.args[0]
    assert "ПОРЯДОК: требуется целое число" in error_text


def test_retry_shows_the_rejected_template_again(monkeypatch) -> None:
    rejected = "НАЗВАНИЕ:\nЧерновик\n\nПОРЯДОК:\nошибка"
    message = _Message()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10, username="admin"),
        message=message,
        answer=AsyncMock(),
    )
    state = _State(
        {"template_mode": "create", "raw_template": rejected},
        TemplateCorrectionStates.input.state,
    )
    monkeypatch.setattr(item_handlers, "Message", _Message)
    monkeypatch.setattr(item_handlers, "edit_or_send_text", AsyncMock())

    asyncio.run(
        item_handlers.item_template_retry(
            callback,
            state,
            AuthService(frozenset({10})),
        )
    )

    assert message.answer.await_args_list[0].args[0] == rejected
    assert message.answer.await_args_list[0].kwargs["parse_mode"] is None


def test_full_edit_previews_then_preserves_id_photo_and_placement(
    monkeypatch,
) -> None:
    current = _item()
    changed = current.model_copy(
        update={"name": "Kiss Me Updated", "photo_file_id": "ignored"}
    )
    message = _Message(serialize_item_template(changed))
    state = _State(
        {
            "template_mode": "edit",
            "category_id": "cocktails",
            "subcategory_id": "classic",
            "item_id": "kiss_me",
        }
    )
    service = SimpleNamespace(
        get_catalog=AsyncMock(return_value=_catalog(current)),
        update_item=AsyncMock(),
    )

    asyncio.run(
        item_handlers.item_template_receive(
            message,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    assert state.current == TemplateConfirmationStates.edit.state
    assert "Предпросмотр полной карточки" in message.answer.await_args.args[0]
    service.update_item.assert_not_awaited()

    callback_message = _Message()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10, username="admin"),
        message=callback_message,
        answer=AsyncMock(),
    )
    monkeypatch.setattr(item_handlers, "Message", _Message)
    monkeypatch.setattr(item_handlers, "show_item_editor", AsyncMock())

    asyncio.run(
        item_handlers.item_full_edit_confirm(
            callback,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    args = service.update_item.await_args.args
    changes = service.update_item.await_args.kwargs
    assert args[1:] == ("cocktails", "classic", "kiss_me")
    assert changes["name"] == "Kiss Me Updated"
    assert "id" not in changes
    assert "photo_file_id" not in changes
    assert state.cleared


def test_template_creation_generates_id_only_after_confirmation(
    monkeypatch,
) -> None:
    draft = _item(name="Новая карта").model_copy(
        update={"id": "ignored", "photo_file_id": None}
    )
    message = _Message(serialize_item_template(draft))
    state = _State(
        {
            "template_mode": "create",
            "category_id": "cocktails",
            "subcategory_id": "classic",
        }
    )
    service = SimpleNamespace(
        get_catalog=AsyncMock(return_value=_catalog()),
        create_item=AsyncMock(),
    )

    asyncio.run(
        item_handlers.item_template_receive(
            message,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    assert state.current == TemplateConfirmationStates.create.state
    assert "novaya_karta" in message.answer.await_args.args[0]
    service.create_item.assert_not_awaited()

    callback_message = _Message()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10, username="admin"),
        message=callback_message,
        answer=AsyncMock(),
    )
    rendered = AsyncMock()
    monkeypatch.setattr(item_handlers, "Message", _Message)
    monkeypatch.setattr(item_handlers, "edit_or_send_text", rendered)

    asyncio.run(
        item_handlers.item_template_create_confirm(
            callback,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    created = service.create_item.await_args.args[3]
    assert created.id == "novaya_karta"
    assert created.photo_file_id is None
    button_texts = {
        button.text
        for row in rendered.await_args.args[2].inline_keyboard
        for button in row
    }
    assert button_texts == {"🖼 Добавить фото", "⏭ Завершить без фото"}
    assert state.cleared


def test_template_cancel_clears_state_without_save() -> None:
    message = _Message("/cancel")
    state = _State(
        {"pending_update": {"name": "Never saved"}},
        TemplateConfirmationStates.create.state,
    )

    asyncio.run(
        common_handlers.handle_admin_cancel_command(
            message,
            state,
            AuthService(frozenset({10})),
        )
    )

    assert state.cleared
    assert "Действие отменено" in message.answer.await_args.args[0]


def test_full_edit_checks_admin_before_reading_item(monkeypatch) -> None:
    message = _Message(user_id=11)
    callback, callback_data = _callback("ea", message, user_id=11)
    service = SimpleNamespace(get_catalog=AsyncMock())
    monkeypatch.setattr(item_handlers, "Message", _Message)

    asyncio.run(
        item_handlers.item_full_edit_start(
            callback,
            callback_data,
            _State(),
            AuthService(frozenset({10})),
            service,
        )
    )

    callback.answer.assert_awaited_once_with(
        "Недостаточно прав", show_alert=True
    )
    service.get_catalog.assert_not_awaited()


def test_command_is_not_parsed_as_template() -> None:
    message = _Message("/unknown")
    state = _State(
        {
            "template_mode": "create",
            "category_id": "cocktails",
            "subcategory_id": "classic",
        },
        TemplateCreateItemStates.input.state,
    )
    service = SimpleNamespace(get_catalog=AsyncMock(), create_item=AsyncMock())

    asyncio.run(
        item_handlers.item_template_receive(
            message,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    assert state.current == TemplateCreateItemStates.input.state
    service.get_catalog.assert_not_awaited()
    service.create_item.assert_not_awaited()
    assert "не записана в шаблон" in message.answer.await_args.args[0]
