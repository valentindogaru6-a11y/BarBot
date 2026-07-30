"""Read-only JSON storage for the catalog."""

import json
from pathlib import Path

from pydantic import ValidationError

from app.models.catalog import Catalog

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog.json"


class CatalogStorageError(RuntimeError):
    """Base error raised while reading catalog data."""


class CatalogFileNotFoundError(CatalogStorageError):
    """Raised when the catalog JSON file does not exist."""


class CatalogJSONDecodeError(CatalogStorageError):
    """Raised when the catalog file contains malformed JSON."""


class CatalogValidationError(CatalogStorageError):
    """Raised when decoded JSON does not match the catalog schema."""


class JsonCatalogStorage:
    """Load and validate a catalog from a JSON file without modifying it."""

    def __init__(self, path: str | Path = DEFAULT_CATALOG_PATH) -> None:
        self.path = Path(path)

    def load(self) -> Catalog:
        """Read, decode, and validate the configured catalog file."""

        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CatalogFileNotFoundError(
                f"Файл каталога не найден: {self.path}"
            ) from exc
        except OSError as exc:
            raise CatalogStorageError(
                f"Не удалось прочитать файл каталога {self.path}: {exc}"
            ) from exc

        try:
            raw_data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CatalogJSONDecodeError(
                f"Некорректный JSON в файле каталога {self.path}: "
                f"строка {exc.lineno}, столбец {exc.colno}"
            ) from exc

        try:
            return Catalog.model_validate(raw_data)
        except ValidationError as exc:
            raise CatalogValidationError(
                f"Структура каталога в файле {self.path} не прошла валидацию: "
                f"{exc.error_count()} ошибок"
            ) from exc


def load_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> Catalog:
    """Convenience function for loading a catalog from ``path``."""

    return JsonCatalogStorage(path).load()
