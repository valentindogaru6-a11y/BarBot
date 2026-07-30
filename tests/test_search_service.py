"""Tests for catalog search business logic."""

from app.models.catalog import Catalog
from app.services.search_service import SearchService


def _result_ids(catalog: Catalog, query: str) -> list[str]:
    return [
        result.item.id
        for result in SearchService(catalog).search(query)
    ]


def test_searches_by_partial_name(catalog: Catalog) -> None:
    assert _result_ids(catalog, "физ") == ["berry-fizz"]


def test_searches_by_tag(catalog: Catalog) -> None:
    assert _result_ids(catalog, "тропич") == ["passion-sour"]


def test_searches_by_ingredient_name(catalog: Catalog) -> None:
    assert _result_ids(catalog, "малиновое") == ["berry-fizz"]


def test_search_is_case_insensitive(catalog: Catalog) -> None:
    assert _result_ids(catalog, "МАРАКУЙЯ") == ["passion-sour"]


def test_searches_only_active_items(catalog: Catalog) -> None:
    assert _result_ids(catalog, "скрытый") == []


def test_searches_only_active_categories(catalog: Catalog) -> None:
    assert _result_ids(catalog, "скрывает позицию") == []


def test_empty_query_returns_no_results(catalog: Catalog) -> None:
    assert _result_ids(catalog, "   ") == []
