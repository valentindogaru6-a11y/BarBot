"""Root model for catalog data."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.category import Category


class Catalog(BaseModel):
    """Versioned catalog document stored in ``data/catalog.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    categories: list[Category] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_globally_unique_ids(self) -> "Catalog":
        """Reject duplicate IDs across all navigation levels and items."""

        seen: set[str] = set()
        for category in self.categories:
            self._remember_id(category.id, seen)
            for item in category.items:
                self._remember_id(item.id, seen)
            for subcategory in category.subcategories:
                self._remember_id(subcategory.id, seen)
                for item in subcategory.items:
                    self._remember_id(item.id, seen)
        return self

    @staticmethod
    def _remember_id(identifier: str, seen: set[str]) -> None:
        if identifier in seen:
            raise ValueError(
                f"Duplicate catalog identifier: {identifier}"
            )
        seen.add(identifier)
