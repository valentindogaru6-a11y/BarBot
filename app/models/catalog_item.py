"""Models for catalog items and their ingredients."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=18,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
]


class Ingredient(BaseModel):
    """An ingredient used by a catalog item."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    amount: str | int | float
    unit: str


class CatalogItem(BaseModel):
    """A universal item that can belong to any catalog category."""

    model_config = ConfigDict(extra="forbid")

    id: StableId
    name: NonEmptyString
    short_description: str | None = None
    description: str | None = None
    photo_file_id: str | None = None
    ingredients: list[Ingredient] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    steps: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sort_order: int = 0
    is_active: bool = True
