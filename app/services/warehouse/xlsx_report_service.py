"""Safe XlsxWriter generation and atomic publication for warehouse reports."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import zipfile
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import xlsxwriter

from app.config import PROJECT_ROOT
from app.models.warehouse.reports import WarehouseReportArtifact

_PUBLICATION_LOCK = threading.Lock()
_FORMULA_PREFIXES = frozenset("=+-@")
_UNIT_LABELS = {"kg": "кг", "l": "л", "pcs": "шт"}


class WarehouseXlsxError(RuntimeError):
    """Raised when an XLSX cannot be generated or safely verified."""


class WarehouseReportFileConflict(WarehouseXlsxError):
    """Raised when a final path contains unexpected bytes."""


def safe_excel_text(value: object) -> str:
    """Remove invalid XML controls and neutralize formula-like text."""

    text = "".join(
        character
        for character in str(value)
        if (
            character in "\t\n\r"
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
        and ord(character) not in {0xFFFE, 0xFFFF}
    )
    stripped = text.lstrip()
    if stripped and stripped[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_xlsx(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0 or not zipfile.is_zipfile(path):
        raise WarehouseXlsxError("Generated file is not a non-empty XLSX")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if (
                "xl/workbook.xml" not in names
                or "[Content_Types].xml" not in names
            ):
                raise WarehouseXlsxError("Generated XLSX structure is incomplete")
    except zipfile.BadZipFile as exc:
        raise WarehouseXlsxError("Generated XLSX is corrupt") from exc


def _write_workbook(
    path: Path,
    lines: list[dict[str, Any]],
    *,
    business_date: str,
) -> None:
    workbook = xlsxwriter.Workbook(
        path,
        {
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    try:
        workbook.set_properties(
            {
                "title": "Warehouse report",
                "created": datetime.combine(
                    date.fromisoformat(business_date),
                    time.min,
                    tzinfo=UTC,
                ),
            }
        )
        header = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#D9E2F3",
                "font_color": "#1F2937",
                "border": 1,
                "valign": "vcenter",
            }
        )
        text_format = workbook.add_format({"valign": "top"})
        wrap_format = workbook.add_format({"valign": "top", "text_wrap": True})
        quantity_format = workbook.add_format(
            {"num_format": "0.######", "valign": "top"}
        )
        date_format = workbook.add_format(
            {"num_format": "dd.mm.yyyy", "valign": "top"}
        )
        specifications = (
            (
                "writeoff",
                "Списание",
                ("Продукт", "Количество", "Единица", "Причина", "Дата смены"),
                (32, 14, 12, 45, 15),
            ),
            (
                "transfer",
                "Перемещение",
                (
                    "Продукт", "Количество", "Единица", "Откуда", "Куда",
                    "Комментарий", "Дата смены",
                ),
                (32, 14, 12, 22, 22, 45, 15),
            ),
        )
        excel_date = datetime.combine(date.fromisoformat(business_date), time.min)
        for sheet_type, sheet_name, headings, widths in specifications:
            worksheet = workbook.add_worksheet(sheet_name)
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, 0, len(headings) - 1)
            worksheet.set_landscape()
            worksheet.fit_to_pages(1, 0)
            worksheet.repeat_rows(0)
            worksheet.set_row(0, 22)
            for column, (heading, width) in enumerate(zip(headings, widths)):
                worksheet.write_string(0, column, heading, header)
                worksheet.set_column(column, column, width)
            sheet_lines = [
                line for line in lines if line["sheet_type"] == sheet_type
            ]
            for excel_row, line in enumerate(sheet_lines, start=1):
                quantity = round(
                    float(Decimal(int(line["quantity_scaled"])) / 1_000_000),
                    6,
                )
                worksheet.write_string(
                    excel_row, 0, safe_excel_text(line["product_name"]), text_format
                )
                worksheet.write_number(excel_row, 1, quantity, quantity_format)
                worksheet.write_string(
                    excel_row,
                    2,
                    safe_excel_text(_UNIT_LABELS[str(line["base_unit"])]),
                    text_format,
                )
                if sheet_type == "writeoff":
                    worksheet.write_string(
                        excel_row,
                        3,
                        safe_excel_text(line.get("reason_or_comment") or ""),
                        wrap_format,
                    )
                    worksheet.write_datetime(
                        excel_row, 4, excel_date, date_format
                    )
                else:
                    worksheet.write_string(
                        excel_row,
                        3,
                        safe_excel_text(line["source_location_name"]),
                        text_format,
                    )
                    worksheet.write_string(
                        excel_row,
                        4,
                        safe_excel_text(line["destination_location_name"]),
                        text_format,
                    )
                    worksheet.write_string(
                        excel_row,
                        5,
                        safe_excel_text(line.get("reason_or_comment") or ""),
                        wrap_format,
                    )
                    worksheet.write_datetime(
                        excel_row, 6, excel_date, date_format
                    )
    finally:
        workbook.close()


class WarehouseXlsxReportService:
    def __init__(
        self,
        reports_path: str | Path,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        self.project_root = project_root.resolve()
        self.reports_path = Path(reports_path).resolve()
        try:
            self.reports_path.relative_to(self.project_root)
        except ValueError as exc:
            raise WarehouseXlsxError("Report directory escapes project root") from exc

    def relative_path(self, business_date: str, version: int) -> str:
        parsed = date.fromisoformat(business_date)
        final = (
            self.reports_path
            / f"{parsed.year:04d}"
            / f"{parsed.month:02d}"
            / f"warehouse_{business_date}_v{version:03d}.xlsx"
        ).resolve()
        try:
            return final.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise WarehouseXlsxError("Final report path escapes project root") from exc

    def _absolute(self, relative_file_path: str) -> Path:
        candidate = Path(relative_file_path)
        if candidate.is_absolute():
            raise WarehouseXlsxError("Absolute report path is forbidden")
        resolved = (self.project_root / candidate).resolve()
        try:
            resolved.relative_to(self.reports_path)
        except ValueError as exc:
            raise WarehouseXlsxError("Report path escapes configured directory") from exc
        return resolved

    def build_final(self, report: dict[str, Any]) -> WarehouseReportArtifact:
        relative = self.relative_path(
            str(report["business_date"]), int(report["version"])
        )
        final_path = self._absolute(relative)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=final_path.parent,
                prefix=f".{final_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
            _write_workbook(
                temporary,
                list(report["lines"]),
                business_date=str(report["business_date"]),
            )
            _validate_xlsx(temporary)
            with temporary.open("rb") as generated:
                os.fsync(generated.fileno())
            generated_hash = _sha256(temporary)
            generated_size = temporary.stat().st_size
            with _PUBLICATION_LOCK:
                if final_path.exists():
                    _validate_xlsx(final_path)
                    if (
                        _sha256(final_path) != generated_hash
                        or final_path.stat().st_size != generated_size
                    ):
                        raise WarehouseReportFileConflict(
                            "Final report path contains different bytes"
                        )
                    temporary.unlink()
                    temporary = None
                else:
                    os.replace(temporary, final_path)
                    temporary = None
                    directory_fd = os.open(final_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            return WarehouseReportArtifact(
                path=final_path,
                relative_file_path=relative,
                file_sha256=generated_hash,
                file_size_bytes=generated_size,
            )
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def build_draft(
        self,
        lines: list[dict[str, Any]],
        *,
        business_date: str,
        directory: str | Path,
    ) -> Path:
        destination = Path(directory) / f"warehouse_draft_{business_date}.xlsx"
        _write_workbook(destination, lines, business_date=business_date)
        _validate_xlsx(destination)
        return destination

    def verify_ready(self, report: dict[str, Any]) -> WarehouseReportArtifact:
        relative = report.get("relative_file_path")
        expected_hash = report.get("file_sha256")
        expected_size = report.get("file_size_bytes")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise WarehouseXlsxError("Ready report metadata is incomplete")
        path = self._absolute(relative)
        _validate_xlsx(path)
        size = path.stat().st_size
        digest = _sha256(path)
        if size != expected_size or digest != expected_hash:
            raise WarehouseReportFileConflict("Ready report hash mismatch")
        return WarehouseReportArtifact(
            path=path,
            relative_file_path=relative,
            file_sha256=digest,
            file_size_bytes=size,
        )
