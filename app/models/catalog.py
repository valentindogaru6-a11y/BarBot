"""Root model for catalog data."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.category import Category


class Catalog(BaseModel):
    """Versioned catalog document stored in ``data/catalog.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    categories: list[Category] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_container_unique_ids(self) -> "Catalog":
        """Reject duplicate IDs within each relevant parent container."""

        self._validate_unique(
            [category.id for category in self.categories],
            "category",
        )
        for category in self.categories:
            self._validate_unique(
                [subcategory.id for subcategory in category.subcategories],
                f"subcategory in {category.id}",
            )
            self._validate_unique(
                [item.id for item in category.items],
                f"item in {category.id}",
            )
            for subcategory in category.subcategories:
                self._validate_unique(
                    [item.id for item in subcategory.items],
                    f"item in {category.id}/{subcategory.id}",
                )
        return self

    @staticmethod
    def _validate_unique(identifiers: list[str], scope: str) -> None:
        seen: set[str] = set()
        for identifier in identifiers:
            if identifier in seen:
                raise ValueError(
                    f"Duplicate {scope} identifier: {identifier}"
                )
            seen.add(identifier)
