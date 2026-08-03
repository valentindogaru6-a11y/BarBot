"""Serialize and parse full catalog-item text templates.

This module deliberately has no Telegram dependencies.  It validates the
entire template before returning a value, so callers cannot accidentally
apply a partially parsed update.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog_item import CatalogItem, Ingredient
from app.services.admin_service import (
    AdminInputError,
    parse_fields,
    parse_ingredients,
    parse_steps,
    parse_tags,
)

SECTION_ORDER = (
    "name",
    "short_description",
    "description",
    "ingredients",
    "fields",
    "steps",
    "tags",
    "sort_order",
    "is_active",
)

SECTION_TITLES = {
    "name": "НАЗВАНИЕ",
    "short_description": "КРАТКОЕ ОПИСАНИЕ",
    "description": "ПОЛНОЕ ОПИСАНИЕ",
    "ingredients": "СОСТАВ",
    "fields": "ХАРАКТЕРИСТИКИ",
    "steps": "ПРИГОТОВЛЕНИЕ",
    "tags": "ТЕГИ",
    "sort_order": "ПОРЯДОК",
    "is_active": "АКТИВНА",
}

_TITLE_TO_SECTION = {
    title.casefold(): section for section, title in SECTION_TITLES.items()
}
_FORBIDDEN_TITLES = {
    "id",
    "stable id",
    "stable_id",
    "технический id",
    "photo_file_id",
    "photo",
    "фото",
    "category_id",
    "category",
    "категория",
    "subcategory_id",
    "subcategory",
    "подкатегория",
}
_HEADER_PATTERN = re.compile(r"^\s*([^:\n]{1,80})\s*:\s*(.*)$")
_LINE_NUMBER_PATTERN = re.compile(r"^Строка (\d+):\s*(.*)$")
_EMPTY_PLACEHOLDERS = {
    "ingredients": "Название | количество | единица",
    "fields": "ключ | значение",
    "steps": "Один шаг на строку",
    "tags": "тег1, тег2",
}


class CardTemplateError(ValueError):
    """Raised with every error found in a full item template."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


class ParsedItemUpdate(BaseModel):
    """Complete set of editable item fields accepted by a template."""

    model_config = ConfigDict(extra="forbid")

    name: str
    short_description: str | None = None
    description: str | None = None
    ingredients: list[Ingredient] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    steps: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sort_order: int = 0
    is_active: bool = True

    def item_changes(self) -> dict:
        """Return only fields that full text editing is allowed to change."""

        return self.model_dump(mode="python")

    def build_item(
        self,
        *,
        item_id: str,
        photo_file_id: str | None = None,
    ) -> CatalogItem:
        """Build a validated item while supplying protected data explicitly."""

        return CatalogItem(
            id=item_id,
            photo_file_id=photo_file_id,
            **self.item_changes(),
        )


def _optional_text(value: str | None) -> str:
    if value is None or not value.strip():
        return "-"
    return value.strip()


def _format_amount(value: str | int | float) -> str:
    return str(value)


def serialize_item_template(item: CatalogItem) -> str:
    """Serialize editable item fields in the exact format accepted by parser."""

    ingredients = "\n".join(
        f"{entry.name} | {_format_amount(entry.amount)} | {entry.unit}"
        for entry in item.ingredients
    ) or "-"
    fields = "\n".join(
        f"{key} | {value}" for key, value in item.fields.items()
    ) or "-"
    steps = "\n".join(item.steps) or "-"
    tags = ", ".join(item.tags) or "-"
    active = "да" if item.is_active else "нет"

    return (
        f"НАЗВАНИЕ:\n{item.name}\n\n"
        f"КРАТКОЕ ОПИСАНИЕ:\n{_optional_text(item.short_description)}\n\n"
        f"ПОЛНОЕ ОПИСАНИЕ:\n{_optional_text(item.description)}\n\n"
        f"СОСТАВ:\n{ingredients}\n\n"
        f"ХАРАКТЕРИСТИКИ:\n{fields}\n\n"
        f"ПРИГОТОВЛЕНИЕ:\n{steps}\n\n"
        f"ТЕГИ:\n{tags}\n\n"
        f"ПОРЯДОК:\n{item.sort_order}\n\n"
        f"АКТИВНА:\n{active}"
    )


def empty_item_template() -> str:
    """Return the copyable template used by template-based item creation."""

    return (
        "НАЗВАНИЕ:\nВведите название\n\n"
        "КРАТКОЕ ОПИСАНИЕ:\n-\n\n"
        "ПОЛНОЕ ОПИСАНИЕ:\n-\n\n"
        "СОСТАВ:\nНазвание | количество | единица\n\n"
        "ХАРАКТЕРИСТИКИ:\nключ | значение\n\n"
        "ПРИГОТОВЛЕНИЕ:\nОдин шаг на строку\n\n"
        "ТЕГИ:\nтег1, тег2\n\n"
        "ПОРЯДОК:\n1\n\n"
        "АКТИВНА:\nда"
    )


def _looks_like_unknown_header(raw_title: str, _remainder: str) -> bool:
    title = raw_title.strip()
    if not title:
        return False
    # The template is a strict sectioned format.  Treat every header-shaped
    # line as structural so misspelled and mixed-case section names cannot be
    # silently absorbed into the preceding value.
    return True


def _section_error(section: str, error: str) -> str:
    title = SECTION_TITLES[section]
    match = _LINE_NUMBER_PATTERN.match(error)
    if match:
        return f"{title}, строка {match.group(1)}: {match.group(2)}"
    return f"{title}: {error}"


def _section_text(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def parse_item_template(text: str) -> ParsedItemUpdate:
    """Parse a complete item template and report all errors at once."""

    errors: list[str] = []
    sections: dict[str, list[str]] = {}
    seen_order: list[str] = []
    current: str | None = None
    ignored_unknown = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        header = _HEADER_PATTERN.match(raw_line)
        if header:
            raw_title, remainder = header.groups()
            normalized_title = raw_title.strip().casefold()
            section = _TITLE_TO_SECTION.get(normalized_title)
            if section is not None:
                ignored_unknown = False
                current = section
                if section in sections:
                    errors.append(
                        f"повторная секция «{SECTION_TITLES[section]}» "
                        f"(строка {line_number})"
                    )
                    current = None
                    continue
                sections[section] = []
                seen_order.append(section)
                if remainder:
                    sections[section].append(remainder)
                continue
            if _looks_like_unknown_header(raw_title, remainder):
                ignored_unknown = True
                current = None
                if normalized_title in _FORBIDDEN_TITLES:
                    errors.append(
                        f"секция «{raw_title.strip()}» не разрешена"
                    )
                else:
                    errors.append(
                        f"неизвестная секция «{raw_title.strip()}»"
                    )
                continue

        if current is not None:
            sections[current].append(raw_line)
        elif raw_line.strip() and not ignored_unknown:
            errors.append(
                f"строка {line_number}: текст находится вне секции"
            )

    missing = [
        SECTION_TITLES[section]
        for section in SECTION_ORDER
        if section not in sections
    ]
    if missing:
        errors.append("отсутствуют секции: " + ", ".join(missing))

    expected_seen_order = [
        section for section in SECTION_ORDER if section in sections
    ]
    if seen_order != expected_seen_order:
        errors.append(
            "секции должны идти в порядке: "
            + " → ".join(SECTION_TITLES[section] for section in SECTION_ORDER)
        )

    values: dict = {}
    name = _section_text(sections.get("name", []))
    if not name or name == "-" or name.casefold() == "введите название":
        errors.append("НАЗВАНИЕ: значение не может быть пустым")
    else:
        values["name"] = name

    for section in ("short_description", "description"):
        value = _section_text(sections.get(section, []))
        values[section] = None if not value or value == "-" else value

    structured_parsers = {
        "ingredients": parse_ingredients,
        "fields": parse_fields,
    }
    for section, parser in structured_parsers.items():
        section_value = _section_text(sections.get(section, []))
        if section_value.casefold() == _EMPTY_PLACEHOLDERS[section].casefold():
            section_value = "-"
        try:
            values[section] = parser(section_value)
        except AdminInputError as exc:
            errors.extend(
                _section_error(section, error) for error in exc.errors
            )

    raw_steps = _section_text(sections.get("steps", []))
    if raw_steps.casefold() == _EMPTY_PLACEHOLDERS["steps"].casefold():
        raw_steps = "-"
    raw_tags = _section_text(sections.get("tags", []))
    if raw_tags.casefold() == _EMPTY_PLACEHOLDERS["tags"].casefold():
        raw_tags = "-"
    values["steps"] = parse_steps(raw_steps)
    values["tags"] = parse_tags(raw_tags)

    raw_sort_order = _section_text(sections.get("sort_order", []))
    try:
        values["sort_order"] = int(raw_sort_order)
    except ValueError:
        errors.append("ПОРЯДОК: требуется целое число")

    raw_active = _section_text(sections.get("is_active", [])).casefold()
    active_values = {
        "да": True,
        "true": True,
        "1": True,
        "нет": False,
        "false": False,
        "0": False,
    }
    if raw_active not in active_values:
        errors.append(
            "АКТИВНА: допустимы да/нет, true/false или 1/0"
        )
    else:
        values["is_active"] = active_values[raw_active]

    if errors:
        raise CardTemplateError(errors)
    return ParsedItemUpdate.model_validate(values)
