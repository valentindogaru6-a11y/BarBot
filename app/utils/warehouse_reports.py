"""Deterministic aggregation helpers for warehouse report snapshots."""

from __future__ import annotations

from typing import Any, Iterable

from app.utils.warehouse import normalize_warehouse_name


def _append_unique(values: list[str], raw_value: object) -> None:
    if raw_value is None:
        return
    value = str(raw_value).strip()
    if value and value not in values:
        values.append(value)


def aggregate_report_lines(
    operations: Iterable[dict[str, Any]],
    *,
    business_date: str,
) -> list[dict[str, Any]]:
    """Aggregate active operations without losing integer scaled precision."""

    writeoffs: dict[tuple[int, str], dict[str, Any]] = {}
    transfers: dict[tuple[int, str, int, int], dict[str, Any]] = {}
    for operation in operations:
        if operation["operation_type"] == "writeoff":
            key = (int(operation["product_id"]), str(operation["base_unit"]))
            groups = writeoffs
            destination = None
            note = operation.get("reason_text")
        else:
            destination_id = operation.get("destination_location_id")
            if destination_id is None:
                continue
            key = (
                int(operation["product_id"]),
                str(operation["base_unit"]),
                int(operation["source_location_id"]),
                int(destination_id),
            )
            groups = transfers
            destination = operation.get("destination_location_name")
            note = operation.get("comment_text")
        group = groups.get(key)
        if group is None:
            group = {
                "sheet_type": str(operation["operation_type"]),
                "product_id": int(operation["product_id"]),
                "product_name": str(operation["product_name_snapshot"]),
                "quantity_scaled": 0,
                "base_unit": str(operation["base_unit"]),
                "notes": [],
                "source_location_name": str(operation["source_location_name"]),
                "destination_location_name": (
                    str(destination) if destination is not None else None
                ),
                "business_date": business_date,
            }
            groups[key] = group
        group["quantity_scaled"] += int(operation["quantity_scaled"])
        _append_unique(group["notes"], note)

    def sort_key(line: dict[str, Any]) -> tuple[str, str, str, int]:
        return (
            normalize_warehouse_name(str(line["product_name"])),
            normalize_warehouse_name(str(line["source_location_name"])),
            normalize_warehouse_name(
                str(line.get("destination_location_name") or "")
            ),
            int(line["product_id"]),
        )

    result: list[dict[str, Any]] = []
    for sheet_groups in (writeoffs, transfers):
        for row_number, group in enumerate(
            sorted(sheet_groups.values(), key=sort_key),
            start=1,
        ):
            result.append(
                {
                    "sheet_type": group["sheet_type"],
                    "row_number": row_number,
                    "product_id": group["product_id"],
                    "product_name": group["product_name"],
                    "quantity_scaled": group["quantity_scaled"],
                    "base_unit": group["base_unit"],
                    "reason_or_comment": "; ".join(group["notes"]) or None,
                    "source_location_name": group["source_location_name"],
                    "destination_location_name": group[
                        "destination_location_name"
                    ],
                    "business_date": group["business_date"],
                }
            )
    return result
