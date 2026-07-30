"""Validated administrative catalog mutations and text parsers."""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeVar

from pydantic import ValidationError

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem, Ingredient
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.audit_service import AuditService
from app.storage.json_storage import JsonCatalogStorage

catalog_write_lock = asyncio.Lock()
T = TypeVar("T")
FilterMode = Literal["all", "active", "hidden"]
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AdminServiceError(RuntimeError):
    """Base error for administrative catalog operations."""


class CatalogConflictError(AdminServiceError):
    """Raised when a stable ID conflicts with an existing entity."""


class ObjectChangedError(AdminServiceError):
    """Raised when a selected entity was removed or changed."""


class ConfirmationRequiredError(AdminServiceError):
    """Raised when a destructive operation lacks explicit confirmation."""


class AdminInputError(AdminServiceError):
    """Raised when structured administrator input cannot be parsed."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True, slots=True)
class AdminActor:
    """Minimal administrator identity safe for audit logging."""

    user_id: int
    username: str | None = None


def parse_ingredients(text: str) -> list[Ingredient]:
    """Parse ``name | amount | unit`` lines without partial results."""

    if not text.strip() or text.strip() == "-":
        return []

    ingredients: list[Ingredient] = []
    errors: list[str] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        if len(parts) != 3:
            errors.append(
                f"Строка {number}: ожидается ровно 3 части через |"
            )
            continue
        name, raw_amount, unit = parts
        if not name:
            errors.append(f"Строка {number}: название не может быть пустым")
            continue
        amount: str | int | float
        if not raw_amount:
            amount = ""
        else:
            normalized_amount = raw_amount.replace(",", ".")
            try:
                numeric = float(normalized_amount)
                amount = int(numeric) if numeric.is_integer() else numeric
            except ValueError:
                amount = raw_amount
        try:
            ingredients.append(
                Ingredient(name=name, amount=amount, unit=unit)
            )
        except ValidationError as exc:
            errors.append(f"Строка {number}: {exc.errors()[0]['msg']}")
    if errors:
        raise AdminInputError(errors)
    return ingredients


def parse_fields(text: str) -> dict[str, str]:
    """Parse unique ``snake_case_key | value`` lines."""

    if not text.strip() or text.strip() == "-":
        return {}

    fields: dict[str, str] = {}
    errors: list[str] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        if len(parts) != 2:
            errors.append(
                f"Строка {number}: ожидается ровно 2 части через |"
            )
            continue
        key, value = parts
        if not FIELD_KEY_PATTERN.fullmatch(key):
            errors.append(
                f"Строка {number}: ключ должен быть lowercase snake_case"
            )
        elif key in fields:
            errors.append(f"Строка {number}: ключ {key!r} повторяется")
        elif not value:
            errors.append(f"Строка {number}: значение не может быть пустым")
        else:
            fields[key] = value
    if errors:
        raise AdminInputError(errors)
    return fields


def parse_steps(text: str) -> list[str]:
    """Parse one preparation step per non-empty line."""

    if not text.strip() or text.strip() == "-":
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_tags(text: str) -> list[str]:
    """Parse comma-separated tags and remove case-insensitive duplicates."""

    if not text.strip() or text.strip() == "-":
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in text.split(","):
        tag = raw_tag.strip()
        normalized = tag.casefold()
        if tag and normalized not in seen:
            seen.add(normalized)
            tags.append(tag)
    return tags


class AdminService:
    """Apply catalog mutations under one shared asynchronous write lock."""

    def __init__(
        self,
        storage: JsonCatalogStorage,
        audit: AuditService,
        lock: asyncio.Lock = catalog_write_lock,
    ) -> None:
        self.storage = storage
        self.audit = audit
        self.lock = lock

    async def get_catalog(self) -> Catalog:
        """Read current catalog without taking the write lock."""

        return await asyncio.to_thread(self.storage.load)

    async def _change(
        self,
        *,
        actor: AdminActor,
        action: str,
        entity_type: str,
        entity_id: str,
        parent_ids: dict[str, str],
        description: str,
        mutate: Callable[[Catalog], None],
    ) -> Catalog:
        async with self.lock:
            try:
                catalog = await asyncio.to_thread(self.storage.load)
                mutate(catalog)
                validated = Catalog.model_validate(
                    catalog.model_dump(mode="python")
                )
                saved = await asyncio.to_thread(
                    self.storage.save,
                    validated,
                )
            except Exception:
                await asyncio.to_thread(
                    self._audit,
                    actor,
                    action,
                    entity_type,
                    entity_id,
                    parent_ids,
                    description,
                    "error",
                )
                raise

            await asyncio.to_thread(
                self._audit,
                actor,
                action,
                entity_type,
                entity_id,
                parent_ids,
                description,
                "success",
            )
            return saved

    def _audit(
        self,
        actor: AdminActor,
        action: str,
        entity_type: str,
        entity_id: str,
        parent_ids: dict[str, str],
        description: str,
        status: Literal["success", "error"],
    ) -> None:
        self.audit.record(
            admin_user_id=actor.user_id,
            username=actor.username,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            parent_ids=parent_ids,
            description=description,
            status=status,
        )

    @staticmethod
    def filter_entities(
        entities: list[T],
        mode: FilterMode,
    ) -> list[T]:
        if mode == "all":
            return entities
        active = mode == "active"
        return [
            entity
            for entity in entities
            if getattr(entity, "is_active") is active
        ]

    @staticmethod
    def _category(catalog: Catalog, category_id: str) -> Category:
        category = next(
            (item for item in catalog.categories if item.id == category_id),
            None,
        )
        if category is None:
            raise ObjectChangedError("Категория уже удалена или изменена")
        return category

    @classmethod
    def _subcategory(
        cls,
        catalog: Catalog,
        category_id: str,
        subcategory_id: str,
    ) -> Subcategory:
        category = cls._category(catalog, category_id)
        subcategory = next(
            (
                item
                for item in category.subcategories
                if item.id == subcategory_id
            ),
            None,
        )
        if subcategory is None:
            raise ObjectChangedError("Подраздел уже удалён или изменён")
        return subcategory

    @classmethod
    def _items(
        cls,
        catalog: Catalog,
        category_id: str,
        subcategory_id: str | None,
    ) -> list[CatalogItem]:
        if subcategory_id is None:
            return cls._category(catalog, category_id).items
        return cls._subcategory(
            catalog,
            category_id,
            subcategory_id,
        ).items

    async def create_category(
        self,
        actor: AdminActor,
        category: Category,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            if any(item.id == category.id for item in catalog.categories):
                raise CatalogConflictError("ID категории уже существует")
            catalog.categories.append(category)

        return await self._change(
            actor=actor,
            action="create",
            entity_type="category",
            entity_id=category.id,
            parent_ids={},
            description="Создана категория",
            mutate=mutate,
        )

    async def update_category(
        self,
        actor: AdminActor,
        category_id: str,
        **changes: Any,
    ) -> Catalog:
        changes.pop("id", None)

        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            updated_data = category.model_dump(mode="python")
            updated_data.update(changes)
            updated = Category.model_validate(updated_data)
            index = catalog.categories.index(category)
            catalog.categories[index] = updated

        return await self._change(
            actor=actor,
            action="update",
            entity_type="category",
            entity_id=category_id,
            parent_ids={},
            description="Изменены поля категории",
            mutate=mutate,
        )

    async def toggle_category(
        self,
        actor: AdminActor,
        category_id: str,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            category.is_active = not category.is_active

        return await self._change(
            actor=actor,
            action="toggle",
            entity_type="category",
            entity_id=category_id,
            parent_ids={},
            description="Изменена видимость категории",
            mutate=mutate,
        )

    async def delete_category(
        self,
        actor: AdminActor,
        category_id: str,
        *,
        confirmed: bool = False,
    ) -> Catalog:
        if not confirmed:
            raise ConfirmationRequiredError("Нужно подтвердить удаление")

        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            catalog.categories.remove(category)

        return await self._change(
            actor=actor,
            action="delete",
            entity_type="category",
            entity_id=category_id,
            parent_ids={},
            description="Удалена категория с вложенными данными",
            mutate=mutate,
        )

    async def create_subcategory(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory: Subcategory,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            if any(
                item.id == subcategory.id
                for item in category.subcategories
            ):
                raise CatalogConflictError("ID подраздела уже существует")
            category.subcategories.append(subcategory)

        return await self._change(
            actor=actor,
            action="create",
            entity_type="subcategory",
            entity_id=subcategory.id,
            parent_ids={"category_id": category_id},
            description="Создан подраздел",
            mutate=mutate,
        )

    async def update_subcategory(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str,
        **changes: Any,
    ) -> Catalog:
        changes.pop("id", None)

        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            subcategory = self._subcategory(
                catalog,
                category_id,
                subcategory_id,
            )
            index = category.subcategories.index(subcategory)
            updated_data = subcategory.model_dump(mode="python")
            updated_data.update(changes)
            category.subcategories[index] = Subcategory.model_validate(
                updated_data
            )

        return await self._change(
            actor=actor,
            action="update",
            entity_type="subcategory",
            entity_id=subcategory_id,
            parent_ids={"category_id": category_id},
            description="Изменены поля подраздела",
            mutate=mutate,
        )

    async def toggle_subcategory(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            subcategory = self._subcategory(
                catalog,
                category_id,
                subcategory_id,
            )
            subcategory.is_active = not subcategory.is_active

        return await self._change(
            actor=actor,
            action="toggle",
            entity_type="subcategory",
            entity_id=subcategory_id,
            parent_ids={"category_id": category_id},
            description="Изменена видимость подраздела",
            mutate=mutate,
        )

    async def delete_subcategory(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str,
        *,
        confirmed: bool = False,
    ) -> Catalog:
        if not confirmed:
            raise ConfirmationRequiredError("Нужно подтвердить удаление")

        def mutate(catalog: Catalog) -> None:
            category = self._category(catalog, category_id)
            subcategory = self._subcategory(
                catalog,
                category_id,
                subcategory_id,
            )
            category.subcategories.remove(subcategory)

        return await self._change(
            actor=actor,
            action="delete",
            entity_type="subcategory",
            entity_id=subcategory_id,
            parent_ids={"category_id": category_id},
            description="Удалён подраздел с вложенными позициями",
            mutate=mutate,
        )

    async def create_item(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item: CatalogItem,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            items = self._items(catalog, category_id, subcategory_id)
            if any(existing.id == item.id for existing in items):
                raise CatalogConflictError(
                    "ID позиции уже существует в выбранном разделе"
                )
            items.append(item)

        return await self._change(
            actor=actor,
            action="create",
            entity_type="item",
            entity_id=item.id,
            parent_ids={
                "category_id": category_id,
                "subcategory_id": subcategory_id or "",
            },
            description="Создана позиция",
            mutate=mutate,
        )

    async def update_item(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
        **changes: Any,
    ) -> Catalog:
        changes.pop("id", None)

        def mutate(catalog: Catalog) -> None:
            items = self._items(catalog, category_id, subcategory_id)
            item = next(
                (existing for existing in items if existing.id == item_id),
                None,
            )
            if item is None:
                raise ObjectChangedError("Позиция уже удалена или изменена")
            index = items.index(item)
            updated_data = item.model_dump(mode="python")
            updated_data.update(changes)
            items[index] = CatalogItem.model_validate(updated_data)

        return await self._change(
            actor=actor,
            action="update",
            entity_type="item",
            entity_id=item_id,
            parent_ids={
                "category_id": category_id,
                "subcategory_id": subcategory_id or "",
            },
            description="Изменены поля позиции",
            mutate=mutate,
        )

    async def toggle_item(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
    ) -> Catalog:
        def mutate(catalog: Catalog) -> None:
            items = self._items(catalog, category_id, subcategory_id)
            item = next(
                (entry for entry in items if entry.id == item_id),
                None,
            )
            if item is None:
                raise ObjectChangedError("Позиция уже удалена или изменена")
            item.is_active = not item.is_active

        return await self._change(
            actor=actor,
            action="toggle",
            entity_type="item",
            entity_id=item_id,
            parent_ids={
                "category_id": category_id,
                "subcategory_id": subcategory_id or "",
            },
            description="Изменена видимость позиции",
            mutate=mutate,
        )

    async def delete_item(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
        *,
        confirmed: bool = False,
    ) -> Catalog:
        if not confirmed:
            raise ConfirmationRequiredError("Нужно подтвердить удаление")

        def mutate(catalog: Catalog) -> None:
            items = self._items(catalog, category_id, subcategory_id)
            item = next(
                (entry for entry in items if entry.id == item_id),
                None,
            )
            if item is None:
                raise ObjectChangedError("Позиция уже удалена или изменена")
            items.remove(item)

        return await self._change(
            actor=actor,
            action="delete",
            entity_type="item",
            entity_id=item_id,
            parent_ids={
                "category_id": category_id,
                "subcategory_id": subcategory_id or "",
            },
            description="Удалена позиция",
            mutate=mutate,
        )

    async def move_item(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
        target_category_id: str,
        target_subcategory_id: str | None,
    ) -> Catalog:
        if (
            category_id == target_category_id
            and subcategory_id == target_subcategory_id
        ):
            raise CatalogConflictError("Позиция уже находится в этом разделе")

        def mutate(catalog: Catalog) -> None:
            source = self._items(catalog, category_id, subcategory_id)
            item = next(
                (entry for entry in source if entry.id == item_id),
                None,
            )
            if item is None:
                raise ObjectChangedError("Позиция уже удалена или изменена")
            target = self._items(
                catalog,
                target_category_id,
                target_subcategory_id,
            )
            if any(entry.id == item_id for entry in target):
                raise CatalogConflictError(
                    "В целевом разделе уже есть позиция с таким ID"
                )
            source.remove(item)
            target.append(item)

        return await self._change(
            actor=actor,
            action="move",
            entity_type="item",
            entity_id=item_id,
            parent_ids={
                "from_category_id": category_id,
                "from_subcategory_id": subcategory_id or "",
                "to_category_id": target_category_id,
                "to_subcategory_id": target_subcategory_id or "",
            },
            description="Позиция перемещена",
            mutate=mutate,
        )

    async def replace_photo(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
        file_id: str,
    ) -> Catalog:
        return await self.update_item(
            actor,
            category_id,
            subcategory_id,
            item_id,
            photo_file_id=file_id,
        )

    async def remove_photo(
        self,
        actor: AdminActor,
        category_id: str,
        subcategory_id: str | None,
        item_id: str,
        *,
        confirmed: bool = False,
    ) -> Catalog:
        if not confirmed:
            raise ConfirmationRequiredError(
                "Нужно подтвердить удаление фотографии"
            )
        return await self.update_item(
            actor,
            category_id,
            subcategory_id,
            item_id,
            photo_file_id=None,
        )
