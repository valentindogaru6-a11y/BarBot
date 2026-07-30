"""Safe HTML formatters for catalog content."""

import json
from html import escape
from typing import Any

from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory

FIELD_LABELS = {
    "glassware": "Бокал",
    "ice": "Лёд",
    "method": "Метод",
    "garnish": "Украшение",
    "country": "Страна",
    "region": "Регион",
    "grape": "Сорт винограда",
    "serving_temperature": "Температура подачи",
    "cup": "Чашка",
    "coffee_weight": "Вес кофе",
    "temperature": "Температура",
}


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def humanize_field_name(key: str) -> str:
    """Translate a known field key or humanize an unknown snake_case key."""

    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    return key.replace("_", " ").strip().capitalize() or "Поле"


def _format_section_heading(
    section: Category | Subcategory,
) -> str:
    emoji = f"{escape(section.emoji.strip())} " if section.emoji else ""
    parts = [f"<b>{emoji}{escape(section.name)}</b>"]
    if section.description and section.description.strip():
        parts.append(escape(section.description.strip()))
    return "\n\n".join(parts)


def format_category_heading(category: Category) -> str:
    """Format a category heading with escaped user-managed content."""

    return _format_section_heading(category)


def format_subcategory_heading(subcategory: Subcategory) -> str:
    """Format a subcategory heading with escaped user-managed content."""

    return _format_section_heading(subcategory)


def format_item_card(item: CatalogItem) -> str:
    """Build a universal HTML item card without empty sections."""

    parts = [f"<b>{escape(item.name)}</b>"]

    for description in (item.short_description, item.description):
        if description and description.strip():
            parts.append(escape(description.strip()))

    ingredients = []
    for ingredient in item.ingredients:
        amount_and_unit = " ".join(
            part
            for part in (
                str(ingredient.amount).strip(),
                ingredient.unit.strip(),
            )
            if part
        )
        suffix = f" — {escape(amount_and_unit)}" if amount_and_unit else ""
        ingredients.append(f"• {escape(ingredient.name)}{suffix}")
    if ingredients:
        parts.append("<b>Состав:</b>\n" + "\n".join(ingredients))

    fields = [
        f"• {escape(humanize_field_name(key))}: "
        f"{escape(_format_value(value))}"
        for key, value in item.fields.items()
        if _has_content(value)
    ]
    if fields:
        parts.append("<b>Характеристики:</b>\n" + "\n".join(fields))

    steps = [
        f"{number}. {escape(step.strip())}"
        for number, step in enumerate(
            (step for step in item.steps if step.strip()),
            start=1,
        )
    ]
    if steps:
        parts.append("<b>Приготовление:</b>\n" + "\n".join(steps))

    tags = [escape(tag.strip()) for tag in item.tags if tag.strip()]
    if tags:
        parts.append("<b>Теги:</b> " + ", ".join(tags))

    return "\n\n".join(parts)
