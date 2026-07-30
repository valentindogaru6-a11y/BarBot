"""Model for a catalog category."""

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog_item import CatalogItem, NonEmptyString, StableId


class Category(BaseModel):
    """A data-driven category containing arbitrary catalog items."""

    model_config = ConfigDict(extra="forbid")

    id: StableId
    name: NonEmptyString
    emoji: str | None = None
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True
    items: list[CatalogItem] = Field(default_factory=list)
