"""Search business logic for the universal catalog."""

from dataclasses import dataclass

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.services.catalog_service import CatalogService
from app.utils.text import normalize_search_text


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A matching item together with its category context."""

    category: Category
    item: CatalogItem


class SearchService:
    """Search active catalog content without Telegram dependencies."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog_service = CatalogService(catalog)

    def search(self, query: str) -> list[SearchResult]:
        """Find active items by text fields, tags, and ingredient names."""

        normalized_query = normalize_search_text(query)
        if not normalized_query:
            return []

        results: list[SearchResult] = []
        for category in self.catalog_service.get_active_categories():
            for item in self.catalog_service.get_active_items(category.id):
                if self._matches(item, normalized_query):
                    results.append(SearchResult(category=category, item=item))

        return results

    @staticmethod
    def _matches(item: CatalogItem, normalized_query: str) -> bool:
        searchable_values = [
            item.name,
            item.short_description or "",
            item.description or "",
            *item.tags,
            *(ingredient.name for ingredient in item.ingredients),
        ]
        return any(
            normalized_query in normalize_search_text(value)
            for value in searchable_values
        )


def search_catalog(catalog: Catalog, query: str) -> list[SearchResult]:
    """Convenience function for a one-off catalog search."""

    return SearchService(catalog).search(query)
