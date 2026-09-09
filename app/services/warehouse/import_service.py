"""Application service for warehouse seed dry-runs and explicit imports."""

from pathlib import Path

from app.models.warehouse.seed import WarehouseSeedImportPlan
from app.services.warehouse.seed_service import load_warehouse_seed
from app.storage.warehouse_seed_database import WarehouseSeedDatabase


class WarehouseSeedImportService:
    def __init__(self, database_path: str | Path) -> None:
        self.repository = WarehouseSeedDatabase(database_path)

    def run(
        self,
        seed_path: str | Path,
        *,
        apply: bool = False,
    ) -> WarehouseSeedImportPlan:
        seed = load_warehouse_seed(seed_path)
        return self.repository.import_seed(seed, apply=apply)
