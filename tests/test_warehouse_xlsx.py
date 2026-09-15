"""ZIP/XML verification of generated XLSX without an Excel reader runtime."""

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from app.services.warehouse import xlsx_report_service
from app.services.warehouse.xlsx_report_service import (
    WarehouseXlsxReportService,
    safe_excel_text,
)

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"x": MAIN}


def _report() -> dict:
    return {
        "business_date": "2026-09-10",
        "version": 1,
        "lines": [
            {
                "sheet_type": "writeoff",
                "row_number": 1,
                "product_id": 1,
                "product_name": "Апельсин",
                "quantity_scaled": 500_000,
                "base_unit": "kg",
                "reason_or_comment": '   =HYPERLINK("bad")\x01',
                "source_location_name": "Бар",
                "destination_location_name": None,
                "business_date": "2026-09-10",
            },
            {
                "sheet_type": "transfer",
                "row_number": 1,
                "product_id": 2,
                "product_name": "+Сахар",
                "quantity_scaled": 250_000,
                "base_unit": "kg",
                "reason_or_comment": "@комментарий",
                "source_location_name": "-Бар",
                "destination_location_name": "=Кухня",
                "business_date": "2026-09-10",
            },
        ],
    }


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.itertext())
        for node in root.findall("x:si", NS)
    ]


def _cells(
    archive: zipfile.ZipFile,
    sheet: str,
) -> dict[str, tuple[str | None, str, str | None]]:
    shared = _shared_strings(archive)
    root = ET.fromstring(archive.read(sheet))
    result = {}
    for cell in root.findall(".//x:c", NS):
        reference = cell.attrib["r"]
        kind = cell.attrib.get("t")
        style = cell.attrib.get("s")
        value_node = cell.find("x:v", NS)
        value = value_node.text if value_node is not None else ""
        if kind == "s":
            value = shared[int(value)]
        result[reference] = (kind, value, style)
    return result


def test_exact_sheets_columns_numeric_date_and_formatting(tmp_path: Path) -> None:
    writer = WarehouseXlsxReportService(
        tmp_path / "reports",
        project_root=tmp_path,
    )
    artifact = writer.build_final(_report())

    with zipfile.ZipFile(artifact.path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [
            sheet.attrib["name"]
            for sheet in workbook.findall(".//x:sheet", NS)
        ]
        assert names == ["Списание", "Перемещение"]
        writeoff = _cells(archive, "xl/worksheets/sheet1.xml")
        transfer = _cells(archive, "xl/worksheets/sheet2.xml")
        assert [writeoff[f"{column}1"][1] for column in "ABCDE"] == [
            "Продукт", "Количество", "Единица", "Причина", "Дата смены"
        ]
        assert [transfer[f"{column}1"][1] for column in "ABCDEFG"] == [
            "Продукт", "Количество", "Единица", "Откуда", "Куда",
            "Комментарий", "Дата смены",
        ]
        assert writeoff["B2"][0] is None and float(writeoff["B2"][1]) == 0.5
        assert writeoff["E2"][0] is None
        assert writeoff["E2"][2] is not None
        assert writeoff["A2"][1] == "Апельсин"
        assert all(b"<f" not in archive.read(name) for name in (
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
        ))

        first_sheet = archive.read("xl/worksheets/sheet1.xml")
        workbook_xml = archive.read("xl/workbook.xml")
        styles = archive.read("xl/styles.xml")
        assert b'pane ySplit="1"' in first_sheet
        assert b'autoFilter ref="A1:E1"' in first_sheet
        assert b'orientation="landscape"' in first_sheet
        assert b'fitToPage="1"' in first_sheet
        assert b'fitToHeight="0"' in first_sheet
        assert b"mergeCell" not in first_sheet
        assert b"0.######" in styles
        assert b"dd.mm.yyyy" in styles
        assert b"$1:$1" in workbook_xml
        assert "Апельсин".encode() in archive.read("xl/sharedStrings.xml")


def test_formula_injection_xml_controls_and_identity_are_absent(tmp_path: Path) -> None:
    writer = WarehouseXlsxReportService(
        tmp_path / "reports",
        project_root=tmp_path,
    )
    artifact = writer.build_final(_report())
    with zipfile.ZipFile(artifact.path) as archive:
        shared = _shared_strings(archive)
        assert "'   =HYPERLINK(\"bad\")" in shared
        assert "'+Сахар" in shared
        assert "'@комментарий" in shared
        assert "'-Бар" in shared
        assert "'=Кухня" in shared
        assert all("\x01" not in value for value in shared)
        combined = b"".join(archive.read(name) for name in archive.namelist())
        assert b"telegram" not in combined.lower()
        assert b"bartender" not in combined.lower()
        assert b"raw message" not in combined.lower()
        assert not any(name.startswith("xl/charts/") for name in archive.namelist())
    assert safe_excel_text(" \t=1") == "' \t=1"


def test_empty_workbook_keeps_both_header_only_sheets(tmp_path: Path) -> None:
    writer = WarehouseXlsxReportService(
        tmp_path / "reports",
        project_root=tmp_path,
    )
    path = writer.build_draft(
        [],
        business_date="2026-09-10",
        directory=tmp_path,
    )
    with zipfile.ZipFile(path) as archive:
        assert set(_cells(archive, "xl/worksheets/sheet1.xml")) == {
            f"{column}1" for column in "ABCDE"
        }
        assert set(_cells(archive, "xl/worksheets/sheet2.xml")) == {
            f"{column}1" for column in "ABCDEFG"
        }


def test_atomic_generation_failure_leaves_no_final_or_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = WarehouseXlsxReportService(
        tmp_path / "reports",
        project_root=tmp_path,
    )

    def fail(path, lines, *, business_date):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("simulated workbook failure")

    monkeypatch.setattr(xlsx_report_service, "_write_workbook", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        writer.build_final(_report())
    assert not list((tmp_path / "reports").rglob("*.xlsx"))
    assert not list((tmp_path / "reports").rglob("*.tmp"))


def test_repeat_publication_verifies_same_file_without_overwrite(
    tmp_path: Path,
) -> None:
    writer = WarehouseXlsxReportService(
        tmp_path / "reports",
        project_root=tmp_path,
    )
    first = writer.build_final(_report())
    first_bytes = first.path.read_bytes()
    second = writer.build_final(_report())
    assert second.path == first.path
    assert second.file_sha256 == first.file_sha256
    assert second.path.read_bytes() == first_bytes
