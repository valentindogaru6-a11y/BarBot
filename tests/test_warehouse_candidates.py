"""Tests for deterministic read-only warehouse candidate extraction."""

import json
import sqlite3
from pathlib import Path

from app.services.warehouse.candidate_extractor import (
    build_candidate_report,
    write_candidate_report,
)
from scripts import preview_warehouse_candidates


def _catalog(path: Path) -> bytes:
    payload = {
        "version": 1,
        "categories": [
            {
                "id": "drinks",
                "name": "Напитки",
                "items": [
                    {
                        "id": "direct",
                        "name": "Прямая карточка",
                        "ingredients": [
                            {"name": "Апельсин", "amount": "10", "unit": "г"},
                            {"name": "Молоко", "amount": "20", "unit": "мл"},
                            {"name": "Микс", "amount": "1", "unit": "г"},
                            {"name": "Биттер", "amount": "по вкусу", "unit": ""},
                            {"name": "Лайм долька", "amount": "0.5", "unit": "шт"},
                            {"name": "Сироп яблочный", "amount": "5", "unit": "мл"},
                        ],
                    }
                ],
                "subcategories": [
                    {
                        "id": "hidden",
                        "name": "Скрытый раздел",
                        "is_active": False,
                        "items": [
                            {
                                "id": "subitem",
                                "name": "Карточка подраздела",
                                "ingredients": [
                                    {"name": " апельсин ", "amount": "2", "unit": "гр"},
                                    {"name": "Микс", "amount": "2", "unit": "мл"},
                                    {"name": "Только скрытый", "amount": "1", "unit": "кг"},
                                    {"name": "Сироп яблочний", "amount": "5", "unit": "мл"},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(content)
    return content


def _by_name(report: dict) -> dict[str, dict]:
    return {entry["normalized_name"]: entry for entry in report["candidates"]}


def test_extracts_direct_subcategory_hidden_and_exact_normalization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.json"
    _catalog(path)
    report = build_candidate_report(path)
    candidates = _by_name(report)

    orange = candidates["апельсин"]
    assert orange["occurrence_count"] == 2
    assert orange["suggested_base_unit"] == "kg"
    assert orange["base_unit_confidence"] == "high"
    assert orange["suggested_aliases"] == ["апельсин"]
    assert {reference["subcategory_id"] for reference in orange["source_references"]} == {
        None,
        "hidden",
    }
    assert report["summary"]["hidden_path_occurrences"] == 4
    hidden = candidates["только скрытый"]
    assert "Ингредиент встречается только в скрытых карточках" in hidden["warnings"]


def test_unit_inference_conflict_blank_and_piece_warning(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _catalog(path)
    report = build_candidate_report(path)
    candidates = _by_name(report)

    assert candidates["молоко"]["suggested_base_unit"] == "l"
    assert candidates["молоко"]["base_unit_confidence"] == "medium"
    assert candidates["биттер"]["suggested_base_unit"] is None
    assert candidates["биттер"]["base_unit_confidence"] == "unresolved"
    assert candidates["микс"]["suggested_base_unit"] is None
    assert report["summary"]["unit_conflicts"] == 1
    assert report["conflicts"][0]["name"] == "Микс"
    assert "Наблюдается дробная часть штуки" in candidates["лайм долька"]["warnings"]


def test_fuzzy_names_are_not_merged_and_report_is_deterministic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.json"
    _catalog(path)

    first = build_candidate_report(path)
    second = build_candidate_report(path)

    assert first == second
    assert "сироп яблочный" in _by_name(first)
    assert "сироп яблочний" in _by_name(first)
    assert first["fuzzy_review_groups"]
    assert "generated_at" not in json.dumps(first, ensure_ascii=False)


def test_owner_requirements_and_review_metadata(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    _catalog(path)
    report = build_candidate_report(path)

    owner = {entry["candidate_id"]: entry for entry in report["owner_required_candidates"]}
    assert owner["owner_ground_coffee"]["daily_quantity"] == {
        "mode": "fixed",
        "value": "0.06",
        "unit": "kg",
    }
    assert owner["owner_pitcher_milk"]["daily_quantity"] == {"mode": "manual"}
    assert {entry["review_status"] for entry in report["candidates"]} == {"pending"}
    assert report["generated_from"][0]["name"] == "catalog.json"


def test_cli_default_is_read_only_and_does_not_open_sqlite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "catalog.json"
    original = _catalog(path)
    before = set(tmp_path.iterdir())

    def reject_sqlite(*_args, **_kwargs):
        raise AssertionError("candidate preview must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", reject_sqlite)
    assert preview_warehouse_candidates.main(["--catalog", str(path)]) == 0

    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before
    assert "Read-only preview" in capsys.readouterr().out


def test_explicit_output_is_pretty_valid_json_and_catalog_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.json"
    original = _catalog(path)
    output = tmp_path / "review.json"
    report = build_candidate_report(path)

    write_candidate_report(report, output)
    first_bytes = output.read_bytes()
    write_candidate_report(build_candidate_report(path), output)

    assert path.read_bytes() == original
    assert output.read_bytes() == first_bytes
    assert json.loads(first_bytes) == report
    assert first_bytes.startswith(b'{\n  "format_version": 1,')
    assert all(entry["review_status"] == "pending" for entry in report["candidates"])
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(path) not in serialized
    assert "telegram_user_id" not in serialized
