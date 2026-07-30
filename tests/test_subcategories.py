"""Tests for the optional subcategory catalog level."""

from app.models.catalog import Catalog
from app.services.catalog_service import CatalogService
from app.services.search_service import SearchService


def _nested_catalog() -> Catalog:
    return Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "simple",
                    "name": "Простой раздел",
                    "items": [
                        {
                            "id": "direct-item",
                            "name": "Прямая позиция",
                            "sort_order": 10,
                        }
                    ],
                },
                {
                    "id": "drinks",
                    "name": "Напитки",
                    "subcategories": [
                        {
                            "id": "inactive-sub",
                            "name": "Скрытый раздел",
                            "sort_order": 0,
                            "is_active": False,
                            "items": [
                                {
                                    "id": "hidden-nested",
                                    "name": "Скрытый можжевельник",
                                }
                            ],
                        },
                        {
                            "id": "second-sub",
                            "name": "Второй",
                            "sort_order": 20,
                            "items": [],
                        },
                        {
                            "id": "first-sub",
                            "name": "Первый",
                            "sort_order": 10,
                            "items": [
                                {
                                    "id": "nested-item",
                                    "name": "Можжевеловый напиток",
                                    "ingredients": [
                                        {
                                            "name": "Джин",
                                            "amount": 50,
                                            "unit": "мл",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        }
    )


def test_category_keeps_backward_compatible_direct_items() -> None:
    service = CatalogService(_nested_catalog())

    assert [item.id for item in service.get_active_items("simple")] == [
        "direct-item"
    ]
    assert service.get_item("simple", "direct-item") is not None


def test_filters_inactive_and_sorts_active_subcategories() -> None:
    service = CatalogService(_nested_catalog())

    subcategories = service.get_active_subcategories("drinks")

    assert [subcategory.id for subcategory in subcategories] == [
        "first-sub",
        "second-sub",
    ]
    assert service.get_active_items("drinks", "inactive-sub") == []


def test_gets_item_by_full_three_level_path() -> None:
    service = CatalogService(_nested_catalog())

    item = service.get_subcategory_item(
        "drinks",
        "first-sub",
        "nested-item",
    )

    assert item is not None
    assert item.name == "Можжевеловый напиток"


def test_search_finds_items_inside_active_subcategories_only() -> None:
    search = SearchService(_nested_catalog())

    results = search.search("можжевел")

    assert [result.item.id for result in results] == ["nested-item"]
    assert results[0].subcategory is not None
    assert results[0].subcategory.id == "first-sub"
    assert search.search("скрытый") == []
