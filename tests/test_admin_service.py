"""Tests for audited, locked administrator catalog mutations."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.admin_service import (
    AdminActor,
    AdminService,
    CatalogConflictError,
    ConfirmationRequiredError,
    catalog_write_lock,
)
from app.services.audit_service import AuditService
from app.services.search_service import SearchService
from app.storage.json_storage import JsonCatalogStorage

ACTOR = AdminActor(user_id=123, username="admin")


def _service(tmp_path: Path) -> tuple[AdminService, Path, Path]:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps({"version": 1, "categories": []}),
        encoding="utf-8",
    )
    audit_path = tmp_path / "logs" / "admin_audit.jsonl"
    service = AdminService(
        JsonCatalogStorage(catalog_path, tmp_path / "backups"),
        AuditService(audit_path),
    )
    return service, catalog_path, audit_path


def test_creates_category_and_audits_success(tmp_path: Path) -> None:
    service, _, audit_path = _service(tmp_path)

    saved = asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    assert saved.categories[0].id == "coffee"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["admin_user_id"] == 123
    assert record["username"] == "admin"
    assert record["status"] == "success"
    assert record["entity_type"] == "category"


def test_conflicting_category_id_audits_error(tmp_path: Path) -> None:
    service, _, audit_path = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    with pytest.raises(CatalogConflictError):
        asyncio.run(
            service.create_category(
                ACTOR,
                Category(id="coffee", name="Другой"),
            )
        )

    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in records] == ["success", "error"]


def test_audit_filesystem_error_does_not_rollback_catalog(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps({"version": 1, "categories": []}),
        encoding="utf-8",
    )
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block", encoding="utf-8")
    service = AdminService(
        JsonCatalogStorage(catalog_path, tmp_path / "backups"),
        AuditService(blocked_parent / "admin_audit.jsonl"),
    )

    saved = asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    assert saved.categories[0].id == "coffee"
    assert JsonCatalogStorage(catalog_path).load().categories[0].id == "coffee"


def test_edits_category_name_without_changing_id(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    saved = asyncio.run(
        service.update_category(
            ACTOR,
            "coffee",
            name="Кофейная карта",
            id="changed",
        )
    )

    assert saved.categories[0].id == "coffee"
    assert saved.categories[0].name == "Кофейная карта"


def test_hides_category_without_deleting_it(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    saved = asyncio.run(service.toggle_category(ACTOR, "coffee"))

    assert len(saved.categories) == 1
    assert not saved.categories[0].is_active


def test_creates_subcategory_and_rejects_duplicate_in_parent(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )
    asyncio.run(
        service.create_subcategory(
            ACTOR,
            "coffee",
            Subcategory(id="hot", name="Горячий кофе"),
        )
    )

    with pytest.raises(CatalogConflictError):
        asyncio.run(
            service.create_subcategory(
                ACTOR,
                "coffee",
                Subcategory(id="hot", name="Дубль"),
            )
        )


def test_edits_subcategory_name_without_changing_id(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(
                id="coffee",
                name="Кофе",
                subcategories=[
                    Subcategory(id="hot", name="Горячий кофе")
                ],
            ),
        )
    )

    saved = asyncio.run(
        service.update_subcategory(
            ACTOR,
            "coffee",
            "hot",
            name="Горячие напитки",
            id="changed",
        )
    )

    subcategory = saved.categories[0].subcategories[0]
    assert subcategory.id == "hot"
    assert subcategory.name == "Горячие напитки"


def test_creates_and_edits_item_and_full_ingredients(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )
    asyncio.run(
        service.create_item(
            ACTOR,
            "coffee",
            None,
            CatalogItem(id="espresso", name="Эспрессо"),
        )
    )

    saved = asyncio.run(
        service.update_item(
            ACTOR,
            "coffee",
            None,
            "espresso",
            name="Двойной эспрессо",
            id="changed",
            ingredients=[
                {"name": "Кофе", "amount": 18, "unit": "г"},
                {"name": "Вода", "amount": 36, "unit": "мл"},
            ],
        )
    )

    item = saved.categories[0].items[0]
    assert item.id == "espresso"
    assert item.name == "Двойной эспрессо"
    assert [ingredient.name for ingredient in item.ingredients] == [
        "Кофе",
        "Вода",
    ]


def test_replaces_and_removes_photo_only_after_confirmation(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(
                id="coffee",
                name="Кофе",
                items=[CatalogItem(id="espresso", name="Эспрессо")],
            ),
        )
    )

    replaced = asyncio.run(
        service.replace_photo(
            ACTOR,
            "coffee",
            None,
            "espresso",
            "telegram-file-id",
        )
    )
    assert replaced.categories[0].items[0].photo_file_id == "telegram-file-id"

    with pytest.raises(ConfirmationRequiredError):
        asyncio.run(
            service.remove_photo(
                ACTOR,
                "coffee",
                None,
                "espresso",
            )
        )

    removed = asyncio.run(
        service.remove_photo(
            ACTOR,
            "coffee",
            None,
            "espresso",
            confirmed=True,
        )
    )
    assert removed.categories[0].items[0].photo_file_id is None


def test_delete_requires_confirmation(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="coffee", name="Кофе"),
        )
    )

    with pytest.raises(ConfirmationRequiredError):
        asyncio.run(service.delete_category(ACTOR, "coffee"))

    assert asyncio.run(service.get_catalog()).categories[0].id == "coffee"


def test_moves_item_between_containers(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(
                id="coffee",
                name="Кофе",
                items=[CatalogItem(id="espresso", name="Эспрессо")],
            ),
        )
    )
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(id="archive", name="Архив"),
        )
    )

    saved = asyncio.run(
        service.move_item(
            ACTOR,
            "coffee",
            None,
            "espresso",
            "archive",
            None,
        )
    )

    assert saved.categories[0].items == []
    assert saved.categories[1].items[0].id == "espresso"


def test_regular_search_hides_inactive_but_admin_filter_sees_it(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    asyncio.run(
        service.create_category(
            ACTOR,
            Category(
                id="coffee",
                name="Кофе",
                items=[
                    CatalogItem(
                        id="secret",
                        name="Скрытый кофе",
                        is_active=False,
                    )
                ],
            ),
        )
    )
    catalog = asyncio.run(service.get_catalog())

    assert SearchService(catalog).search("скрытый") == []
    hidden = AdminService.filter_entities(
        catalog.categories[0].items,
        "hidden",
    )
    assert [item.id for item in hidden] == ["secret"]


def test_shared_asyncio_lock_serializes_multiple_services(
    tmp_path: Path,
) -> None:
    first, catalog_path, audit_path = _service(tmp_path)
    second = AdminService(
        JsonCatalogStorage(catalog_path, tmp_path / "backups"),
        AuditService(audit_path),
    )

    async def create_both() -> Catalog:
        await asyncio.gather(
            first.create_category(
                ACTOR,
                Category(id="first", name="Первый"),
            ),
            second.create_category(
                ACTOR,
                Category(id="second", name="Второй"),
            ),
        )
        return await first.get_catalog()

    saved = asyncio.run(create_both())

    assert first.lock is catalog_write_lock
    assert second.lock is catalog_write_lock
    assert {category.id for category in saved.categories} == {
        "first",
        "second",
    }


def test_model_rejects_duplicate_ids_in_each_container() -> None:
    with pytest.raises(ValidationError, match="Duplicate category"):
        Catalog(
            version=1,
            categories=[
                Category(id="same", name="Один"),
                Category(id="same", name="Два"),
            ],
        )

    with pytest.raises(ValidationError, match="Duplicate subcategory"):
        Catalog(
            version=1,
            categories=[
                Category(
                    id="parent",
                    name="Родитель",
                    subcategories=[
                        Subcategory(id="same", name="Один"),
                        Subcategory(id="same", name="Два"),
                    ],
                )
            ],
        )

    with pytest.raises(ValidationError, match="Duplicate item"):
        Catalog(
            version=1,
            categories=[
                Category(
                    id="parent",
                    name="Родитель",
                    items=[
                        CatalogItem(id="same", name="Один"),
                        CatalogItem(id="same", name="Два"),
                    ],
                )
            ],
        )
