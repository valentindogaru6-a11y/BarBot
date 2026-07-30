"""Business operations for browsing the universal catalog."""

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory


def _category_sort_key(category: Category) -> tuple[int, str, str]:
    return category.sort_order, category.name.casefold(), category.id


def _item_sort_key(item: CatalogItem) -> tuple[int, str, str]:
    return item.sort_order, item.name.casefold(), item.id


def _subcategory_sort_key(
    subcategory: Subcategory,
) -> tuple[int, str, str]:
    return (
        subcategory.sort_order,
        subcategory.name.casefold(),
        subcategory.id,
    )


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

    def get_active_subcategories(
        self,
        category_id: str,
    ) -> list[Subcategory]:
        """Return active subcategories ordered by configured sort order."""

        category = self.get_category(category_id)
        if category is None:
            return []

        return sorted(
            (
                subcategory
                for subcategory in category.subcategories
                if subcategory.is_active
            ),
            key=_subcategory_sort_key,
        )

    def get_subcategory(
        self,
        category_id: str,
        subcategory_id: str,
    ) -> Subcategory | None:
        """Return a subcategory by its stable IDs, including an inactive one."""

        category = self.get_category(category_id)
        if category is None:
            return None

        return next(
            (
                subcategory
                for subcategory in category.subcategories
                if subcategory.id == subcategory_id
            ),
            None,
        )

    def get_active_items(
        self,
        category_id: str,
        subcategory_id: str | None = None,
    ) -> list[CatalogItem]:
        """Return active direct or subcategory items ordered by sort order."""

        if subcategory_id is None:
            category = self.get_category(category_id)
            if category is None:
                return []
            items = category.items
        else:
            subcategory = self.get_subcategory(category_id, subcategory_id)
            if subcategory is None or not subcategory.is_active:
                return []
            items = subcategory.items

        return sorted(
            (item for item in items if item.is_active),
            key=_item_sort_key,
        )

    def get_item(
        self,
        category_id: str,
        item_id: str,
        subcategory_id: str | None = None,
    ) -> CatalogItem | None:
        """Return an item from a category or one of its subcategories."""

        if subcategory_id is None:
            category = self.get_category(category_id)
            if category is None:
                return None
            items = category.items
        else:
            subcategory = self.get_subcategory(category_id, subcategory_id)
            if subcategory is None:
                return None
            items = subcategory.items

        return next((item for item in items if item.id == item_id), None)

    def get_subcategory_item(
        self,
        category_id: str,
        subcategory_id: str,
        item_id: str,
    ) -> CatalogItem | None:
        """Return an item using the full three-level navigation path."""

        return self.get_item(
            category_id,
            item_id,
            subcategory_id=subcategory_id,
        )
