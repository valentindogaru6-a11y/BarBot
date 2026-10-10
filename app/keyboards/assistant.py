"""Inline keyboards for bounded assistant candidate selection."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import AssistantCandidateCallback
from app.services.search_service import SearchResult
from app.utils.button_labels import search_result_button_labels


def build_assistant_candidates_keyboard(
    results: Sequence[SearchResult],
) -> InlineKeyboardMarkup:
    """Build buttons containing only stable local catalog identifiers."""

    builder = InlineKeyboardBuilder()
    labels = search_result_button_labels(results)
    for result, label in zip(results, labels, strict=True):
        builder.button(
            text=label,
            callback_data=AssistantCandidateCallback(
                category_id=result.category.id,
                item_id=result.item.id,
                subcategory_id=(
                    result.subcategory.id if result.subcategory else "_"
                ),
            ),
        )
    builder.adjust(1)
    return builder.as_markup()
