"""Regression tests for expired Telegram callback handling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.bot import router_names
from app.handlers.admin.common import require_admin_callback
from app.handlers.admin.items import item_edit_start
from app.handlers.admin.users import _require_callback as require_user_admin_callback
from app.handlers.common import (
    answer_callback_safely,
    edit_or_send_text,
    is_expired_callback_error,
)
from app.handlers.start import handle_unexpected_error
from app.services.auth_service import AuthService


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        AnswerCallbackQuery(callback_query_id="fake-callback"),
        message,
    )


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: query is too old",
        "Bad Request: response timeout expired",
        "Bad Request: query ID is invalid",
    ],
)
def test_expired_callback_variants_are_recognized(message: str) -> None:
    assert is_expired_callback_error(_bad_request(message))


def test_unrelated_errors_are_not_expired_callbacks() -> None:
    assert not is_expired_callback_error(
        _bad_request("Bad Request: message to edit not found")
    )
    assert not is_expired_callback_error(RuntimeError("callback failed"))


def test_expiry_text_from_non_callback_method_is_not_suppressed() -> None:
    error = TelegramBadRequest(
        EditMessageText(chat_id=1, message_id=1, text="safe"),
        "Bad Request: query is too old",
    )

    assert not is_expired_callback_error(error)


def test_safe_answer_accepts_fresh_callback() -> None:
    callback = SimpleNamespace(answer=AsyncMock())

    answered = asyncio.run(answer_callback_safely(callback))

    assert answered
    callback.answer.assert_awaited_once_with()


@pytest.mark.parametrize(
    "error",
    [
        _bad_request("Bad Request: message to edit not found"),
        RuntimeError("callback failed"),
    ],
)
def test_safe_answer_preserves_unexpected_errors(error: Exception) -> None:
    callback = SimpleNamespace(answer=AsyncMock(side_effect=error))

    with pytest.raises(type(error), match=str(error)):
        asyncio.run(answer_callback_safely(callback))

    callback.answer.assert_awaited_once_with()


def test_admin_guard_stops_on_expired_callback_without_retry() -> None:
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        answer=AsyncMock(
            side_effect=_bad_request("Bad Request: query is too old")
        ),
    )

    allowed = asyncio.run(
        require_admin_callback(callback, AuthService(frozenset({10})))
    )

    assert not allowed
    callback.answer.assert_awaited_once_with()


def test_admin_denial_still_answers_with_alert() -> None:
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=11),
        answer=AsyncMock(),
    )

    allowed = asyncio.run(
        require_admin_callback(callback, AuthService(frozenset({10})))
    )

    assert not allowed
    callback.answer.assert_awaited_once_with(
        "Недостаточно прав",
        show_alert=True,
    )


def test_user_admin_guard_stops_on_expired_callback() -> None:
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        answer=AsyncMock(
            side_effect=_bad_request("Bad Request: query is too old")
        ),
    )

    allowed = asyncio.run(
        require_user_admin_callback(
            callback,
            AuthService(frozenset({10})),
        )
    )

    assert not allowed
    callback.answer.assert_awaited_once_with()


def test_item_edit_stale_callback_never_reaches_business_logic() -> None:
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        answer=AsyncMock(
            side_effect=_bad_request("Bad Request: query ID is invalid")
        ),
    )
    callback_data = SimpleNamespace(
        action="en",
        category_id="category",
        subcategory_id="-",
        item_id="item",
    )
    state = SimpleNamespace(
        set_state=AsyncMock(),
        set_data=AsyncMock(),
    )
    service = SimpleNamespace(get_catalog=AsyncMock())

    asyncio.run(
        item_edit_start(
            callback,
            callback_data,
            state,
            AuthService(frozenset({10})),
            service,
        )
    )

    callback.answer.assert_awaited_once_with()
    service.get_catalog.assert_not_awaited()
    state.set_state.assert_not_awaited()
    state.set_data.assert_not_awaited()


def test_global_handler_does_not_retry_expired_callback() -> None:
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    event = SimpleNamespace(
        exception=_bad_request("Bad Request: response timeout expired"),
        update=SimpleNamespace(callback_query=callback, message=None),
    )

    handled = asyncio.run(handle_unexpected_error(event))

    assert handled
    callback.answer.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


def test_global_handler_keeps_normal_handling_for_unknown_bad_request() -> None:
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    event = SimpleNamespace(
        exception=_bad_request("Bad Request: message to edit not found"),
        update=SimpleNamespace(callback_query=callback, message=None),
    )

    handled = asyncio.run(handle_unexpected_error(event))

    assert handled
    callback.answer.assert_awaited_once_with(
        "Произошла ошибка. Попробуйте ещё раз.",
        show_alert=True,
    )
    callback.message.answer.assert_not_awaited()


def test_message_not_modified_remains_safely_ignored() -> None:
    message = SimpleNamespace(
        photo=None,
        from_user=SimpleNamespace(is_bot=True),
        edit_text=AsyncMock(
            side_effect=TelegramBadRequest(
                EditMessageText(chat_id=1, message_id=1, text="safe"),
                "Bad Request: message is not modified",
            )
        ),
        answer=AsyncMock(),
    )

    asyncio.run(edit_or_send_text(message, "safe"))

    message.answer.assert_not_awaited()


def test_other_edit_failure_still_falls_back_to_new_message() -> None:
    message = SimpleNamespace(
        photo=None,
        from_user=SimpleNamespace(is_bot=True),
        edit_text=AsyncMock(
            side_effect=TelegramBadRequest(
                EditMessageText(chat_id=1, message_id=1, text="safe"),
                "Bad Request: message to edit not found",
            )
        ),
        answer=AsyncMock(),
    )

    asyncio.run(edit_or_send_text(message, "safe"))

    message.answer.assert_awaited_once_with("safe", reply_markup=None)


def test_admin_and_warehouse_router_order_is_unchanged() -> None:
    disabled = router_names(warehouse_enabled=False)
    enabled = router_names(warehouse_enabled=True)

    assert disabled[-1] == "app.handlers.admin"
    assert enabled[-1] == "app.handlers.admin"
    assert "app.handlers.warehouse" not in disabled
    assert "app.handlers.warehouse" in enabled
