"""Tests for safe atomic catalog persistence and backups."""

import json
from pathlib import Path

import pytest

from app.models.catalog import Catalog
from app.models.category import Category
from app.storage.json_storage import (
    CatalogValidationError,
    CatalogWriteError,
    JsonCatalogStorage,
)


def _write_initial(path: Path) -> None:
    path.write_text(
        json.dumps({"version": 1, "categories": []}),
        encoding="utf-8",
    )


def test_atomic_write_saves_valid_pretty_utf8_json(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _write_initial(path)
    storage = JsonCatalogStorage(path, tmp_path / "backups")
    catalog = Catalog(
        version=1,
        categories=[Category(id="coffee", name="Кофе")],
    )

    saved = storage.save(catalog)

    assert saved.categories[0].name == "Кофе"
    text = path.read_text(encoding="utf-8")
    assert '"Кофе"' in text
    assert "\\u041a" not in text
    assert text.endswith("\n")
    assert not list(tmp_path.glob(".catalog.json.*.tmp"))


def test_original_file_survives_atomic_replace_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "catalog.json"
    _write_initial(path)
    original = path.read_bytes()
    storage = JsonCatalogStorage(path, tmp_path / "backups")

    def fail_replace(source, target) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        "app.storage.json_storage.os.replace",
        fail_replace,
    )

    with pytest.raises(CatalogWriteError):
        storage.save(
            Catalog(
                version=1,
                categories=[Category(id="coffee", name="Кофе")],
            )
        )

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".catalog.json.*.tmp"))


def test_creates_backup_before_write(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _write_initial(path)
    backup_dir = tmp_path / "backups"
    storage = JsonCatalogStorage(path, backup_dir)

    storage.save(Catalog(version=1, categories=[]))

    backups = list(backup_dir.glob("catalog-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {
        "version": 1,
        "categories": [],
    }


def test_backup_rotation_keeps_at_most_thirty(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _write_initial(path)
    backup_dir = tmp_path / "backups"
    storage = JsonCatalogStorage(path, backup_dir)

    for _ in range(35):
        storage.save(Catalog(version=1, categories=[]))

    assert len(list(backup_dir.glob("catalog-*.json"))) == 30


def test_cannot_write_invalid_constructed_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _write_initial(path)
    original = path.read_bytes()
    storage = JsonCatalogStorage(path, tmp_path / "backups")
    invalid = Catalog.model_construct(version=0, categories=[])

    with pytest.raises(CatalogValidationError):
        storage.save(invalid)

    assert path.read_bytes() == original
