"""Safe read and atomic write storage for the JSON catalog."""

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.models.catalog import Catalog

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog.json"
DEFAULT_BACKUP_DIR = Path(__file__).resolve().parents[2] / "backups" / "catalog"
MAX_BACKUPS = 30


class CatalogStorageError(RuntimeError):
    """Base error raised while reading catalog data."""


class CatalogFileNotFoundError(CatalogStorageError):
    """Raised when the catalog JSON file does not exist."""


class CatalogJSONDecodeError(CatalogStorageError):
    """Raised when the catalog file contains malformed JSON."""


class CatalogValidationError(CatalogStorageError):
    """Raised when decoded JSON does not match the catalog schema."""


class CatalogBackupError(CatalogStorageError):
    """Raised when a required catalog backup cannot be created."""


class CatalogWriteError(CatalogStorageError):
    """Raised when atomic catalog persistence fails."""


class CatalogVerificationError(CatalogStorageError):
    """Raised when the persisted catalog cannot be validated again."""


class JsonCatalogStorage:
    """Load, validate, back up, and atomically persist a JSON catalog."""

    def __init__(
        self,
        path: str | Path = DEFAULT_CATALOG_PATH,
        backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)

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

    def save(self, catalog: Catalog) -> Catalog:
        """Validate, back up, atomically save, and verify a catalog."""

        try:
            validated = Catalog.model_validate(catalog.model_dump(mode="python"))
        except ValidationError as exc:
            raise CatalogValidationError(
                "Нельзя сохранить каталог: данные не прошли валидацию"
            ) from exc

        backup_path = self._create_backup()
        try:
            self._atomic_write(validated)
            try:
                return self.load()
            except CatalogStorageError as exc:
                self._restore_backup(backup_path)
                raise CatalogVerificationError(
                    "Сохранённый каталог не прошёл контрольную проверку"
                ) from exc
        except CatalogStorageError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise CatalogWriteError(
                "Не удалось атомарно сохранить каталог"
            ) from exc

    def _create_backup(self) -> Path | None:
        if not self.path.exists():
            return None

        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self.backup_dir / f"catalog-{timestamp}.json"
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.path, backup_path)
            with backup_path.open("rb") as backup_file:
                os.fsync(backup_file.fileno())
            self._prune_backups()
            return backup_path
        except OSError as exc:
            backup_path.unlink(missing_ok=True)
            raise CatalogBackupError(
                "Не удалось создать резервную копию каталога"
            ) from exc

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("catalog-*.json"))
        for old_backup in backups[:-MAX_BACKUPS]:
            old_backup.unlink()

    def _atomic_write(self, catalog: Catalog) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(
                    catalog.model_dump(mode="json"),
                    temp_file,
                    ensure_ascii=False,
                    indent=2,
                )
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise CatalogWriteError(
                "Не удалось атомарно сохранить каталог"
            ) from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _restore_backup(self, backup_path: Path | None) -> None:
        if backup_path is None:
            self.path.unlink(missing_ok=True)
            return
        restore_path: Path | None = None
        try:
            with backup_path.open("rb") as source:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.restore.",
                    suffix=".tmp",
                    delete=False,
                ) as restore_file:
                    restore_path = Path(restore_file.name)
                    shutil.copyfileobj(source, restore_file)
                    restore_file.flush()
                    os.fsync(restore_file.fileno())
            os.replace(restore_path, self.path)
            restore_path = None
        except OSError as exc:
            raise CatalogVerificationError(
                "Проверка не пройдена, восстановление backup не удалось"
            ) from exc
        finally:
            if restore_path is not None:
                restore_path.unlink(missing_ok=True)


def load_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> Catalog:
    """Convenience function for loading a catalog from ``path``."""

    return JsonCatalogStorage(path).load()
