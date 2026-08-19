"""Read-only phonetic-alias coverage for the real JSON catalog."""

from pathlib import Path

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.services.catalog_service import CatalogService
from app.services.voice_alias_service import (
    VoiceAliasService,
    contains_cyrillic,
    contains_latin,
)
from app.storage.json_storage import JsonCatalogStorage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"


def _all_items(catalog: Catalog) -> list[CatalogItem]:
    return [
        item
        for category in catalog.categories
        for item in (
            list(category.items)
            + [
                subcategory_item
                for subcategory in category.subcategories
                for subcategory_item in subcategory.items
            ]
        )
    ]


def _active_items(catalog: Catalog) -> list[CatalogItem]:
    service = CatalogService(catalog)
    items: list[CatalogItem] = []
    for category in service.get_active_categories():
        items.extend(service.get_active_items(category.id))
        for subcategory in service.get_active_subcategories(category.id):
            items.extend(
                service.get_active_items(category.id, subcategory.id)
            )
    return items


def test_every_active_real_item_has_searchable_alias_coverage() -> None:
    before_bytes = CATALOG_PATH.read_bytes()
    catalog = JsonCatalogStorage(CATALOG_PATH).load()
    all_items = _all_items(catalog)
    protected_before = [
        (item.id, item.photo_file_id) for item in all_items
    ]
    service = VoiceAliasService()
    active_items = _active_items(catalog)
    latin_items = [item for item in active_items if contains_latin(item.name)]

    for item in active_items:
        aliases = service.build_aliases(item)
        assert aliases, item.name
        if contains_latin(item.name):
            assert any(
                contains_cyrillic(alias) and not contains_latin(alias)
                for alias in aliases
            ), item.name

    protected_after = [
        (item.id, item.photo_file_id) for item in _all_items(catalog)
    ]
    assert len(active_items) >= 24
    assert len(latin_items) >= 24
    assert protected_after == protected_before
    assert CATALOG_PATH.read_bytes() == before_bytes
