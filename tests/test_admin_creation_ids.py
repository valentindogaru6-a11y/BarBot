"""Tests for automatic IDs in administrator creation FSMs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.admin.categories import category_create_name
from app.handlers.admin.items import create_item_name
from app.handlers.admin.states import (
    CreateCategoryStates,
    CreateItemStates,
    CreateSubcategoryStates,
)
from app.handlers.admin.subcategories import subcategory_create_name
from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.auth_service import AuthService


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}
        self.current = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **changes) -> None:
        self.data.update(changes)

    async def set_state(self, state) -> None:
        self.current = state


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=10),
        answer=AsyncMock(),
    )


def _service(catalog: Catalog) -> SimpleNamespace:
    return SimpleNamespace(get_catalog=AsyncMock(return_value=catalog))


def test_creation_state_groups_have_no_manual_id_step() -> None:
    assert not hasattr(CreateCategoryStates, "stable_id")
    assert not hasattr(CreateSubcategoryStates, "stable_id")
    assert not hasattr(CreateItemStates, "stable_id")


def test_category_name_generates_id_without_manual_input() -> None:
    state = _State()

    asyncio.run(
        category_create_name(
            _message("Кофейная карта"),
            state,
            AuthService(frozenset({10})),
            _service(Catalog(version=1, categories=[])),
        )
    )

    assert state.data == {
        "name": "Кофейная карта",
        "stable_id": "kofeynaya_karta",
    }
    assert state.current == CreateCategoryStates.emoji


def test_subcategory_name_generates_unique_id_in_parent() -> None:
    state = _State({"category_id": "cocktails"})
    catalog = Catalog(
        version=1,
        categories=[
            Category(
                id="cocktails",
                name="Коктейли",
                subcategories=[
                    Subcategory(id="klassika", name="Старое название")
                ],
            )
        ],
    )

    asyncio.run(
        subcategory_create_name(
            _message("Классика"),
            state,
            AuthService(frozenset({10})),
            _service(catalog),
        )
    )

    assert state.data["stable_id"] == "klassika_2"
    assert state.current == CreateSubcategoryStates.emoji


def test_item_name_generates_unique_id_in_selected_placement() -> None:
    state = _State(
        {
            "category_id": "cocktails",
            "subcategory_id": "classic",
        }
    )
    catalog = Catalog(
        version=1,
        categories=[
            Category(
                id="cocktails",
                name="Коктейли",
                subcategories=[
                    Subcategory(
                        id="classic",
                        name="Классика",
                        items=[
                            CatalogItem(id="negroni", name="Старый Negroni")
                        ],
                    )
                ],
            )
        ],
    )

    asyncio.run(
        create_item_name(
            _message("Negroni"),
            state,
            AuthService(frozenset({10})),
            _service(catalog),
        )
    )

    assert state.data["stable_id"] == "negroni_2"
    assert state.current == CreateItemStates.short_description
