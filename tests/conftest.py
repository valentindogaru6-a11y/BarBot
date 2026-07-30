"""Shared catalog fixtures."""

import pytest

from app.models.catalog import Catalog


@pytest.fixture
def catalog() -> Catalog:
    """Catalog with active and inactive entities in unsorted order."""

    return Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "inactive-category",
                    "name": "Скрытая категория",
                    "sort_order": 0,
                    "is_active": False,
                    "items": [
                        {
                            "id": "hidden-by-category",
                            "name": "Скрытая категория скрывает позицию",
                            "tags": ["секрет"],
                        }
                    ],
                },
                {
                    "id": "coffee",
                    "name": "Кофе",
                    "sort_order": 20,
                    "is_active": True,
                    "items": [],
                },
                {
                    "id": "cocktails",
                    "name": "Коктейли",
                    "sort_order": 10,
                    "is_active": True,
                    "items": [
                        {
                            "id": "inactive-item",
                            "name": "Скрытый сауэр",
                            "description": "Не должен находиться",
                            "tags": ["секрет"],
                            "sort_order": 0,
                            "is_active": False,
                        },
                        {
                            "id": "berry-fizz",
                            "name": "Ягодный физз",
                            "short_description": "Лёгкий тестовый напиток",
                            "ingredients": [
                                {
                                    "name": "Малиновое пюре",
                                    "amount": 20,
                                    "unit": "мл",
                                }
                            ],
                            "tags": ["ягодный", "демо"],
                            "sort_order": 20,
                            "is_active": True,
                        },
                        {
                            "id": "passion-sour",
                            "name": "Маракуйя Сауэр",
                            "description": "Кисло-сладкий демонстрационный коктейль",
                            "ingredients": [
                                {
                                    "name": "Пюре маракуйи",
                                    "amount": "по вкусу",
                                    "unit": "",
                                }
                            ],
                            "tags": ["тропический"],
                            "sort_order": 10,
                            "is_active": True,
                        },
                    ],
                },
            ],
        }
    )
