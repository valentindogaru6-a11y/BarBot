"""Shared short, readable, and collision-resistant Telegram button labels."""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from os.path import commonprefix

from app.services.search_service import SearchResult

MAX_BUTTON_LABEL_LENGTH = 48
ELLIPSIS = "…"
_SEPARATOR = " — "


def _clean(value: str) -> str:
    return " ".join(value.strip().split())


def _rstrip_punctuation(value: str) -> str:
    while value and (
        value[-1].isspace()
        or unicodedata.category(value[-1]).startswith("P")
    ):
        value = value[:-1]
    return value


def truncate_button_label(
    value: str,
    max_length: int = MAX_BUTTON_LABEL_LENGTH,
) -> str:
    """Shorten display-only text on a word boundary and append an ellipsis."""

    if max_length < 1:
        raise ValueError("max_length must be positive")
    text = _clean(value)
    if len(text) <= max_length:
        return text
    if max_length == 1:
        return ELLIPSIS

    available = max_length - len(ELLIPSIS)
    candidate = text[:available]
    boundary = candidate.rfind(" ")
    if boundary >= max(4, available // 2):
        candidate = candidate[:boundary]
    candidate = _rstrip_punctuation(candidate)
    if not candidate:
        candidate = text[:available]
    return candidate + ELLIPSIS


def _with_emoji(name: str, emoji: str | None) -> str:
    prefix = f"{emoji.strip()} " if emoji and emoji.strip() else ""
    return prefix + _clean(name)


def _fit_with_suffix(text: str, suffix: str) -> str:
    if not suffix:
        return truncate_button_label(text)
    suffix = _clean(suffix)
    separator = " "
    reserved = len(separator) + len(suffix)
    if reserved >= MAX_BUTTON_LABEL_LENGTH:
        return truncate_button_label(f"{text}{separator}{suffix}")
    return (
        truncate_button_label(text, MAX_BUTTON_LABEL_LENGTH - reserved)
        + separator
        + suffix
    )


def category_button_label(name: str, emoji: str | None = None) -> str:
    """Format an emoji and category name without hierarchy context."""

    return truncate_button_label(_with_emoji(name, emoji))


def subcategory_button_label(name: str, emoji: str | None = None) -> str:
    """Format an emoji and subcategory name without its parent path."""

    return truncate_button_label(_with_emoji(name, emoji))


def item_button_label(name: str) -> str:
    """Keep an item name first and omit already-selected parent context."""

    return truncate_button_label(name)


def admin_entity_button_label(
    name: str,
    is_active: bool,
    emoji: str | None = None,
) -> str:
    """Keep the shared entity label while preserving the hidden marker."""

    base = _with_emoji(name, emoji)
    return _fit_with_suffix(base, "[скрыто]" if not is_active else "")


def _distinctive_fragment(values: Sequence[str], index: int) -> str:
    folded = [_clean(value).casefold() for value in values]
    shared = commonprefix(folded)
    source = _clean(values[index])
    tail = source[len(shared) :].strip(" ·—-_/,.()[]")
    if tail:
        return truncate_button_label(tail, 16)
    return str(index + 1)


def _prepend_distinctive_fragment(label: str, fragment: str) -> str:
    prefix = f"{fragment} · "
    main = label
    suffix = ""
    if _SEPARATOR in label:
        main, context = label.rsplit(_SEPARATOR, 1)
        suffix = _SEPARATOR + context
    elif label.endswith(" [скрыто]"):
        main = label[: -len(" [скрыто]")]
        suffix = " [скрыто]"
    available = MAX_BUTTON_LABEL_LENGTH - len(prefix) - len(suffix)
    if available < 1:
        return truncate_button_label(prefix + label)
    return prefix + truncate_button_label(main, available) + suffix


def _ensure_unique(labels: list[str], sources: Sequence[str]) -> list[str]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        groups[label.casefold()].append(index)

    result = list(labels)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        group_sources = [sources[index] for index in indexes]
        for group_index, label_index in enumerate(indexes):
            fragment = _distinctive_fragment(group_sources, group_index)
            result[label_index] = _prepend_distinctive_fragment(
                labels[label_index],
                fragment,
            )

    # Exact duplicate source text has no semantic distinguishing fragment.
    # A small ordinal is preferable to two indistinguishable Telegram buttons.
    remaining: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(result):
        remaining[label.casefold()].append(index)
    for indexes in remaining.values():
        if len(indexes) < 2:
            continue
        for ordinal, label_index in enumerate(indexes, start=1):
            result[label_index] = _prepend_distinctive_fragment(
                result[label_index],
                str(ordinal),
            )
    return result


def admin_entity_button_labels(
    entities: Sequence[tuple[str, bool, str | None]],
) -> list[str]:
    """Format an admin list with statuses and collision-safe labels."""

    labels = [
        admin_entity_button_label(name, is_active, emoji)
        for name, is_active, emoji in entities
    ]
    return _ensure_unique(labels, [name for name, _, _ in entities])


def item_button_labels(names: Sequence[str]) -> list[str]:
    """Format one item screen and keep truncated labels distinguishable."""

    labels = [item_button_label(name) for name in names]
    return _ensure_unique(labels, names)


def search_result_button_label(
    item_name: str,
    category_name: str,
    subcategory_name: str | None = None,
) -> str:
    """Put the result name first and reserve room for minimal context."""

    context = _clean(subcategory_name or category_name)
    context = truncate_button_label(context, 18)
    suffix = _SEPARATOR + context
    available = MAX_BUTTON_LABEL_LENGTH - len(suffix)
    if available < 12:
        context = truncate_button_label(context, 12)
        suffix = _SEPARATOR + context
        available = MAX_BUTTON_LABEL_LENGTH - len(suffix)
    return truncate_button_label(item_name, available) + suffix


def search_result_button_labels(
    results: Sequence[SearchResult],
) -> list[str]:
    """Build short contextual labels and disambiguate duplicates as a batch."""

    name_counts = Counter(_clean(result.item.name).casefold() for result in results)
    primary_contexts = [
        _clean(
            result.subcategory.name
            if result.subcategory is not None
            else result.category.name
        )
        for result in results
    ]
    contexts = list(primary_contexts)
    for index, result in enumerate(results):
        name_key = _clean(result.item.name).casefold()
        if name_counts[name_key] < 2:
            continue
        same_name_contexts = {
            primary_contexts[other_index].casefold()
            for other_index, other in enumerate(results)
            if _clean(other.item.name).casefold() == name_key
        }
        if len(same_name_contexts) == name_counts[name_key]:
            continue
        if result.subcategory is not None:
            contexts[index] = (
                f"{result.category.name} · {result.subcategory.name}"
            )
        else:
            contexts[index] = result.category.name

    labels = [
        search_result_button_label(
            result.item.name,
            result.category.name,
            contexts[index],
        )
        for index, result in enumerate(results)
    ]
    uniqueness_sources = [
        (
            contexts[index]
            if name_counts[_clean(result.item.name).casefold()] > 1
            else result.item.name
        )
        for index, result in enumerate(results)
    ]
    return _ensure_unique(labels, uniqueness_sources)


def user_button_label(display_name: str, username: str | None = None) -> str:
    """Show only a person's display name and optional username."""

    name = _clean(display_name)
    if not username:
        return truncate_button_label(name)
    handle = "@" + username.strip().lstrip("@")
    return _fit_with_suffix(name, f"· {handle}")
