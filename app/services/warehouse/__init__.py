"""Lazy public exports for warehouse services.

Avoid importing the whole service graph when Python loads one warehouse
submodule; storage modules must remain independently importable.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "WarehouseNomenclatureService",
    "WarehouseValidationError",
    "normalize_warehouse_name",
    "WarehouseIntakeError",
    "WarehouseOperationsService",
    "WarehouseClosingService",
    "WarehouseReportService",
]

_EXPORTS = {
    "WarehouseNomenclatureService": (
        "app.services.warehouse.nomenclature_service", "WarehouseNomenclatureService"
    ),
    "WarehouseValidationError": (
        "app.services.warehouse.nomenclature_service", "WarehouseValidationError"
    ),
    "normalize_warehouse_name": (
        "app.services.warehouse.normalization", "normalize_warehouse_name"
    ),
    "WarehouseIntakeError": (
        "app.services.warehouse.operations_service", "WarehouseIntakeError"
    ),
    "WarehouseOperationsService": (
        "app.services.warehouse.operations_service", "WarehouseOperationsService"
    ),
    "WarehouseClosingService": (
        "app.services.warehouse.closing_service", "WarehouseClosingService"
    ),
    "WarehouseReportService": (
        "app.services.warehouse.report_service", "WarehouseReportService"
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
