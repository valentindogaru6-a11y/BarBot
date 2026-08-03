"""Pagination controls appear only when navigation is actually needed."""

from app.keyboards.admin_catalog import build_admin_categories_keyboard
from app.keyboards.catalog import build_categories_keyboard
from app.keyboards.user_admin import build_users_list_keyboard
from app.models.category import Category


def _texts(markup) -> set[str]:
    return {
        button.text
        for row in markup.inline_keyboard
        for button in row
    }


def _categories(count: int) -> list[Category]:
    return [
        Category(id=f"category-{number}", name=f"Раздел {number}")
        for number in range(count)
    ]


def test_user_catalog_hides_single_page_counter() -> None:
    assert "1/1" not in _texts(build_categories_keyboard(_categories(5)))


def test_user_catalog_keeps_real_pagination() -> None:
    texts = _texts(build_categories_keyboard(_categories(9)))

    assert "1/2" in texts
    assert "▶️" in texts


def test_admin_catalog_hides_single_page_counter() -> None:
    assert "1/1" not in _texts(
        build_admin_categories_keyboard(_categories(5))
    )


def test_admin_catalog_keeps_real_pagination() -> None:
    texts = _texts(build_admin_categories_keyboard(_categories(9)))

    assert "1/2" in texts
    assert "▶️" in texts


def test_user_management_hides_single_page_counter() -> None:
    markup = build_users_list_keyboard(
        [],
        role="user",
        page=0,
        total_pages=1,
    )

    assert "1/1" not in _texts(markup)


def test_user_management_keeps_real_pagination() -> None:
    markup = build_users_list_keyboard(
        [],
        role="user",
        page=0,
        total_pages=2,
    )
    texts = _texts(markup)

    assert "1/2" in texts
    assert "▶️" in texts
