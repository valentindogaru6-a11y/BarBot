"""Business operations for browsing the universal catalog."""

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category


def _category_sort_key(category: Category) -> tuple[int, str, str]:
    return category.sort_order, category.name.casefold(), category.id


def _item_sort_key(item: CatalogItem) -> tuple[int, str, str]:
    return item.sort_order, item.name.casefold(), item.id


class CatalogService:
    """Provide catalog queries independently of storage and Telegram."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def get_active_categories(self) -> list[Category]:
        """Return active categories ordered by their configured sort order."""

        return sorted(
            (
                category
                for category in self.catalog.categories
                if category.is_active
            ),
            key=_category_sort_key,
        )

    def get_category(self, category_id: str) -> Category | None:
        """Return a category by its stable ID, including an inactive one."""

        return next(
            (
                category
                for category in self.catalog.categories
                if category.id == category_id
            ),
            None,
        )

    def get_active_items(self, category_id: str) -> list[CatalogItem]:
        """Return active items of a category ordered by sort order."""

        category = self.get_category(category_id)
        if category is None:
            return []

        return sorted(
            (item for item in category.items if item.is_active),
            key=_item_sort_key,
        )

    def get_item(self, category_id: str, item_id: str) -> CatalogItem | None:
        """Return an item by category ID and stable item ID."""

        category = self.get_category(category_id)
        if category is None:
            return None

        return next((item for item in category.items if item.id == item_id), None)
