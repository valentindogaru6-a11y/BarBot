"""Root model for catalog data."""

from pydantic import BaseModel, ConfigDict, Field

from app.models.category import Category


class Catalog(BaseModel):
    """Versioned catalog document stored in ``data/catalog.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    categories: list[Category] = Field(default_factory=list)
