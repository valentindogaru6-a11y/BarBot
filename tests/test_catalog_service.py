"""Tests for catalog browsing business logic."""

from app.models.catalog import Catalog
from app.services.catalog_service import CatalogService


def test_filters_inactive_categories(catalog: Catalog) -> None:
    categories = CatalogService(catalog).get_active_categories()

    assert {category.id for category in categories} == {"cocktails", "coffee"}


def test_sorts_categories_by_sort_order(catalog: Catalog) -> None:
    categories = CatalogService(catalog).get_active_categories()

    assert [category.id for category in categories] == ["cocktails", "coffee"]


def test_filters_inactive_items(catalog: Catalog) -> None:
    items = CatalogService(catalog).get_active_items("cocktails")

    assert {item.id for item in items} == {"passion-sour", "berry-fizz"}


def test_sorts_items_by_sort_order(catalog: Catalog) -> None:
    items = CatalogService(catalog).get_active_items("cocktails")

    assert [item.id for item in items] == ["passion-sour", "berry-fizz"]


def test_gets_item_by_category_and_item_id(catalog: Catalog) -> None:
    item = CatalogService(catalog).get_item("cocktails", "berry-fizz")

    assert item is not None
    assert item.name == "Ягодный физз"


def test_returns_none_for_unknown_item(catalog: Catalog) -> None:
    item = CatalogService(catalog).get_item("cocktails", "unknown")

    assert item is None
