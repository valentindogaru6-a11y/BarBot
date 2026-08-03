"""Storage adapters for catalog data."""

from app.storage.json_storage import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_BACKUP_DIR,
    CatalogBackupError,
    CatalogFileNotFoundError,
    CatalogJSONDecodeError,
    CatalogStorageError,
    CatalogVerificationError,
    CatalogWriteError,
    CatalogValidationError,
    JsonCatalogStorage,
    load_catalog,
)
from app.storage.user_database import UserDatabase, UserDatabaseError

__all__ = [
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_BACKUP_DIR",
    "CatalogBackupError",
    "CatalogFileNotFoundError",
    "CatalogJSONDecodeError",
    "CatalogStorageError",
    "CatalogVerificationError",
    "CatalogWriteError",
    "CatalogValidationError",
    "JsonCatalogStorage",
    "load_catalog",
    "UserDatabase",
    "UserDatabaseError",
]
