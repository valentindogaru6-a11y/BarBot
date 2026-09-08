"""FSM-based Telegram catalog search."""

import logging
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.handlers.common import catalog_error_message, edit_or_send_text, load_catalog
from app.keyboards.callbacks import MainCallback, SearchCallback
from app.keyboards.catalog import build_search_results_keyboard
from app.keyboards.main_menu import (
    build_main_menu_keyboard,
    build_search_prompt_keyboard,
    build_search_retry_keyboard,
)
from app.middlewares.access import CatalogAccessFilter
from app.services.search_service import SearchService
from app.services.auth_service import AuthService
from app.storage.json_storage import CatalogStorageError
from app.utils.text import normalize_search_text

router = Router(name=__name__)
router.message.filter(CatalogAccessFilter())
router.callback_query.filter(CatalogAccessFilter())
logger = logging.getLogger(__name__)

SEARCH_PROMPT = (
    "<b>Поиск</b>\n\n"
    "Введите название позиции, ингредиент или тег.\n"
    "Для выхода используйте /cancel."
)
SEARCH_RESULT_LIMIT = 20


class SearchStates(StatesGroup):
    """Search conversation states."""

    waiting_for_query = State()


async def _start_search(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_for_query)
    await edit_or_send_text(
        message,
        SEARCH_PROMPT,
        build_search_prompt_keyboard(),
    )


@router.message(Command("search"))
async def handle_search_command(
    message: Message,
    state: FSMContext,
) -> None:
    """Open search from the Telegram commands menu."""

    await state.clear()
    await _start_search(message, state)


@router.callback_query(MainCallback.filter(F.action == "search"))
async def handle_search_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Ask the user for a search query."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_search(callback.message, state)


@router.callback_query(SearchCallback.filter(F.action == "retry"))
async def handle_search_retry(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Start another search."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_search(callback.message, state)


@router.callback_query(SearchCallback.filter(F.action == "cancel"))
async def handle_search_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Cancel search through the inline button."""

    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            "Поиск отменён.",
            build_main_menu_keyboard(
                can_manage_catalog=auth_service.is_admin(callback.from_user.id)
            ),
        )


@router.message(Command("cancel"), SearchStates.waiting_for_query)
async def handle_search_cancel_command(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Cancel search through the command."""

    await state.clear()
    await message.answer(
        "Поиск отменён.",
        reply_markup=build_main_menu_keyboard(
            can_manage_catalog=bool(
                message.from_user
                and auth_service.is_admin(message.from_user.id)
            )
        ),
    )


@router.message(SearchStates.waiting_for_query)
async def handle_search_query(
    message: Message,
    state: FSMContext,
    catalog_path: Path,
    auth_service: AuthService,
) -> None:
    """Execute a normalized search and show at most twenty results."""

    query = normalize_search_text(message.text or "")
    if not query:
        await message.answer(
            "Запрос не должен быть пустым. Попробуйте ещё раз.",
            reply_markup=build_search_prompt_keyboard(),
        )
        return

    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError as exc:
        logger.error("Could not load catalog from %s: %s", catalog_path, exc)
        await state.clear()
        await message.answer(
            catalog_error_message(exc),
            reply_markup=build_main_menu_keyboard(
                can_manage_catalog=bool(
                    message.from_user
                    and auth_service.is_admin(message.from_user.id)
                )
            ),
        )
        return

    results = SearchService(catalog).search(query)[:SEARCH_RESULT_LIMIT]
    await state.clear()
    if not results:
        await message.answer(
            f"По запросу «{escape(query)}» ничего не найдено.",
            reply_markup=build_search_retry_keyboard(),
        )
        return

    await message.answer(
        f"<b>Результаты поиска:</b> {escape(query)}\n"
        f"Найдено: {len(results)}",
        reply_markup=build_search_results_keyboard(results),
    )
