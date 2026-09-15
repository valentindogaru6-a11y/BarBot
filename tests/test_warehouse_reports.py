"""Final/draft warehouse report lifecycle on temporary SQLite and files."""

import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.seed_service import load_warehouse_seed
from app.services.warehouse.xlsx_report_service import (
    WarehouseReportFileConflict,
    WarehouseXlsxError,
    WarehouseXlsxReportService,
)
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_reports_database import (
    EmptyWarehouseReportError,
    WarehouseReportsDatabase,
    WarehouseReportsError,
)
from app.storage.warehouse_seed_database import WarehouseSeedDatabase
from app.utils.warehouse_reports import aggregate_report_lines

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture(scope="module")
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse-reports") / "template.db"
    UserDatabase(path).initialize()
    seed = load_warehouse_seed(ROOT / "source" / "warehouse_products_seed.json")
    WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    return path


@pytest.fixture
def environment(database_template: Path, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    path = project / "warehouse.db"
    shutil.copyfile(database_template, path)
    users = UserService(UserDatabase(path))
    for user_id in (10, 20, 30, 40):
        users.ensure_user(user_id)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    users.set_role(30, Role.ACCOUNTANT, actor_user_id=10)
    access = AuthService(frozenset({1}), users)
    clock = FrozenClock()
    operations = WarehouseOperationsService(
        WarehouseOperationsDatabase(path), access, clock=clock
    )
    closing = WarehouseClosingService(
        WarehouseClosingDatabase(path), operations, access, clock=clock
    )
    xlsx = WarehouseXlsxReportService(
        project / "reports" / "warehouse",
        project_root=project,
    )
    reports = WarehouseReportService(
        WarehouseReportsDatabase(path),
        operations,
        access,
        project / "reports" / "warehouse",
        clock=clock,
        xlsx_service=xlsx,
    )
    return path, project, users, operations, closing, reports


def _product(operations, name: str):
    return next(
        item for item in operations.repository.products()
        if item.official_name == name
    )


def _close(closing: WarehouseClosingService, actor: int = 20) -> int:
    session = closing.start(actor_user_id=actor)
    for requirement in session["requirements"]:
        if requirement["status"] == "pending":
            session = closing.mark_not_applicable(
                int(session["id"]),
                int(requirement["id"]),
                "Остатков не было",
                actor_user_id=actor,
            )
    session = closing.resume(int(session["id"]), actor_user_id=actor)
    closing.finalize(
        int(session["id"]),
        expected_revision=int(session["revision"]),
        actor_user_id=actor,
    )
    return int(session["shift_id"])


def test_report_row_order_is_normalized_and_deterministic() -> None:
    def operation(product_id: int, name: str, destination_id: int) -> dict:
        return {
            "operation_type": "transfer",
            "product_id": product_id,
            "product_name_snapshot": name,
            "quantity_scaled": 1,
            "base_unit": "pcs",
            "source_location_id": 1,
            "source_location_name": "Бар",
            "destination_location_id": destination_id,
            "destination_location_name": f"Место {destination_id}",
            "comment_text": None,
        }

    lines = aggregate_report_lines(
        [operation(3, "Яблоко", 2), operation(2, "Апельсин", 3),
         operation(1, "Апельсин", 2)],
        business_date="2026-09-10",
    )

    assert [
        (line["product_name"], line["destination_location_name"], line["product_id"])
        for line in lines
    ] == [
        ("Апельсин", "Место 2", 1),
        ("Апельсин", "Место 3", 2),
        ("Яблоко", "Место 2", 3),
    ]


def test_final_snapshot_exact_aggregation_and_closing_to_closed(environment) -> None:
    path, _, _, operations, closing, reports = environment
    orange = _product(operations, "Апельсин")
    for quantity, reason in (
        ("0.15", "испорчен"),
        ("0.35", "остаток"),
        ("0.10", "испорчен"),
    ):
        operations.create_manual_operation(
            actor_user_id=20,
            operation_type="writeoff",
            product_id=orange.id,
            quantity_text=quantity,
            reason_text=reason,
        )
    deleted = operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=orange.id,
        quantity_text="1",
        reason_text="не попадёт",
    )
    operations.delete_operation(
        int(deleted["id"]),
        expected_revision=int(deleted["revision"]),
        actor_user_id=20,
        deletion_reason="ошибка",
    )
    sugar = _product(operations, "Сахар")
    destinations = operations.destination_options(actor_user_id=20)
    for destination, comment in (
        (destinations[0], "первый"),
        (destinations[0], "первый"),
        (destinations[1], "второй"),
    ):
        operations.create_manual_operation(
            actor_user_id=20,
            operation_type="transfer",
            product_id=sugar.id,
            quantity_text="0.2",
            destination_location_id=destination.id,
            comment_text=comment,
        )
    shift_id = _close(closing)

    result = reports.build_final(shift_id, actor_user_id=20)

    report = result["report"]
    assert report["status"] == "ready"
    assert report["version"] == 1
    assert report["shift_status"] == "closed"
    assert report["writeoff_count"] == 1
    assert report["transfer_count"] == 2
    writeoff = next(
        line for line in report["lines"] if line["sheet_type"] == "writeoff"
    )
    assert writeoff["quantity_scaled"] == 600_000
    assert writeoff["reason_or_comment"] == "испорчен; остаток"
    transfers = [
        line for line in report["lines"] if line["sheet_type"] == "transfer"
    ]
    assert {line["quantity_scaled"] for line in transfers} == {200_000, 400_000}
    assert all(line["destination_location_name"] for line in transfers)
    artifact = result["artifact"]
    assert artifact.path.name == "warehouse_2026-09-10_v001.xlsx"
    assert artifact.path.relative_to(result["artifact"].path.parents[2])
    assert artifact.file_size_bytes == artifact.path.stat().st_size
    with sqlite3.connect(path) as connection:
        shift = connection.execute(
            "SELECT status, closed_by, closed_at_utc FROM warehouse_shifts WHERE id=?",
            (shift_id,),
        ).fetchone()
        assert shift[0] == "closed" and shift[1] == 20 and shift[2]
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_audit_events "
            "WHERE event_type='warehouse_report_ready'"
        ).fetchone()[0] == 1


def test_snapshot_is_immutable_and_independent_of_future_renames(environment) -> None:
    path, _, _, operations, closing, reports = environment
    orange = _product(operations, "Апельсин")
    operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=orange.id,
        quantity_text="0.2",
    )
    result = reports.build_final(_close(closing), actor_user_id=20)
    report_id = int(result["report"]["id"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE warehouse_products SET official_name='Новое имя' WHERE id=?",
            (orange.id,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE warehouse_report_lines SET product_name='Подмена' "
                "WHERE report_id=?",
                (report_id,),
            )
    stored = reports.report_card(report_id, actor_user_id=30)
    assert stored["lines"][0]["product_name"] == "Апельсин"


def test_generation_failure_retry_reuses_v001_and_snapshot(
    environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, _, _, closing, reports = environment
    shift_id = _close(closing)
    original = reports.xlsx.build_final

    def fail(_report):
        raise RuntimeError("writer failed")

    monkeypatch.setattr(reports.xlsx, "build_final", fail)
    with pytest.raises(RuntimeError, match="writer failed"):
        reports.build_final(shift_id, actor_user_id=20, allow_empty=True)
    failed = reports.repository.report_for_shift(shift_id)
    assert failed is not None
    assert failed["status"] == "failed" and failed["version"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM warehouse_shifts WHERE id=?", (shift_id,)
        ).fetchone()[0] == "closing"

    monkeypatch.setattr(reports.xlsx, "build_final", original)
    recovered = reports.build_final(
        shift_id, actor_user_id=20, allow_empty=True
    )
    assert recovered["report"]["id"] == failed["id"]
    assert recovered["report"]["version"] == 1
    assert recovered["report"]["shift_status"] == "closed"


def test_duplicate_and_two_concurrent_builders_return_same_report(environment) -> None:
    path, _, _, operations, closing, reports = environment
    orange = _product(operations, "Апельсин")
    operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=orange.id,
        quantity_text="0.1",
    )
    shift_id = _close(closing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: reports.build_final(shift_id, actor_user_id=20),
                range(2),
            )
        )
    assert results[0]["report"]["id"] == results[1]["report"]["id"]
    repeated = reports.build_final(shift_id, actor_user_id=20)
    assert repeated["report"]["id"] == results[0]["report"]["id"]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?", (shift_id,)
        ).fetchone()[0] == 1


def test_ready_report_can_finish_interrupted_shift_transition(environment) -> None:
    path, _, _, _, closing, reports = environment
    result = reports.build_final(
        _close(closing), actor_user_id=20, allow_empty=True
    )
    report = result["report"]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE warehouse_shifts SET status='closing', closed_at_utc=NULL, "
            "closed_by=NULL WHERE id=?",
            (report["shift_id"],),
        )
        connection.commit()

    recovered = reports.build_final(
        int(report["shift_id"]), actor_user_id=20, allow_empty=True
    )
    assert recovered["report"]["shift_status"] == "closed"
    assert recovered["report"]["id"] == report["id"]


def test_published_file_with_building_state_is_recovered(environment) -> None:
    _, _, _, _, closing, reports = environment
    shift_id = _close(closing)
    building = reports.repository.prepare_initial_report(
        shift_id,
        actor_user_id=20,
        now=reports._now(),
        allow_empty=True,
        authorize=lambda: reports.access.require_warehouse_mutation(20),
    )
    published = reports.xlsx.build_final(building)
    assert reports.repository.report_for_shift(shift_id)["status"] == "building"

    recovered = reports.build_final(shift_id, actor_user_id=20)

    assert recovered["report"]["id"] == building["id"]
    assert recovered["report"]["version"] == 1
    assert recovered["report"]["shift_status"] == "closed"
    assert recovered["artifact"].file_sha256 == published.file_sha256


def test_hash_verification_missing_corrupt_and_path_traversal(environment) -> None:
    _, project, _, _, closing, reports = environment
    result = reports.build_final(
        _close(closing), actor_user_id=20, allow_empty=True
    )
    report_id = int(result["report"]["id"])
    path = result["artifact"].path
    original = path.read_bytes()
    path.write_bytes(b"not-an-xlsx")
    with pytest.raises(WarehouseXlsxError):
        reports.download(report_id, actor_user_id=30)
    path.write_bytes(original)
    assert reports.download(report_id, actor_user_id=30)["artifact"].path == path
    path.unlink()
    with pytest.raises(WarehouseXlsxError):
        reports.download(report_id, actor_user_id=30)
    with pytest.raises(WarehouseXlsxError, match="escapes"):
        reports.xlsx._absolute("../outside.xlsx")
    assert not (project.parent / "outside.xlsx").exists()


def test_draft_is_ephemeral_does_not_close_or_create_history(environment) -> None:
    path, _, _, operations, _, reports = environment
    orange = _product(operations, "Апельсин")
    operations.create_manual_operation(
        actor_user_id=20,
        operation_type="writeoff",
        product_id=orange.id,
        quantity_text="0.1",
    )
    with tempfile.TemporaryDirectory() as directory:
        draft = reports.draft(actor_user_id=30, directory=directory)
        draft_path = draft["path"]
        assert draft_path.name == "warehouse_draft_2026-09-10.xlsx"
        assert draft_path.exists()
        assert draft["writeoff_count"] == 1
    assert not draft_path.exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM warehouse_shifts"
        ).fetchone()[0] == "open"


def test_empty_report_requires_explicit_confirmation(environment) -> None:
    path, _, _, _, closing, reports = environment
    shift_id = _close(closing)
    with pytest.raises(EmptyWarehouseReportError):
        reports.build_final(shift_id, actor_user_id=20)
    assert reports.repository.report_for_shift(shift_id) is None
    result = reports.build_final(
        shift_id, actor_user_id=20, allow_empty=True
    )
    assert result["report"]["writeoff_count"] == 0
    assert result["report"]["transfer_count"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM warehouse_shifts WHERE id=?", (shift_id,)
        ).fetchone()[0] == "closed"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM warehouse_reports WHERE id=?",
                (result["report"]["id"],),
            )


def test_accountant_download_but_cannot_build(environment) -> None:
    _, _, _, _, closing, reports = environment
    shift_id = _close(closing)
    with pytest.raises(AccessDeniedError):
        reports.build_final(shift_id, actor_user_id=30, allow_empty=True)
    ready = reports.build_final(shift_id, actor_user_id=20, allow_empty=True)
    downloaded = reports.download(
        int(ready["report"]["id"]), actor_user_id=30
    )
    assert downloaded["artifact"].file_sha256 == ready["artifact"].file_sha256


def test_history_paginates_and_forged_location_is_hidden(environment) -> None:
    path, _, _, operations, _, reports = environment
    settings = operations.repository.runtime_settings()
    locations = operations.repository.locations()
    other_source = next(
        location for location in locations
        if location.id != int(settings["source_location_id"])
    )
    with sqlite3.connect(path) as connection:
        for day in range(1, 10):
            cursor = connection.execute(
                """
                INSERT INTO warehouse_shifts (
                    source_location_id, business_date, timezone_snapshot,
                    cutoff_snapshot, status, started_at_utc,
                    scheduled_close_at_utc, closed_at_utc, closed_by,
                    revision, created_at_utc, updated_at_utc
                ) VALUES (?, ?, 'Europe/Kyiv', '23:00', 'closed', 'now',
                          'later', 'done', 20, 1, 'now', 'now')
                """,
                (
                    int(settings["source_location_id"]),
                    f"2026-08-{day:02d}",
                ),
            )
            connection.execute(
                """
                INSERT INTO warehouse_reports (
                    shift_id, version, report_kind, status, relative_file_path,
                    file_sha256, file_size_bytes, created_at_utc, created_by,
                    ready_at_utc
                ) VALUES (?, 1, 'final', 'ready', ?, ?, 1, 'now', 20, 'now')
                """,
                (
                    cursor.lastrowid,
                    f"reports/warehouse/history-{day}.xlsx",
                    f"{day:064x}",
                ),
            )
        forged_shift = connection.execute(
            """
            INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot,
                cutoff_snapshot, status, started_at_utc,
                scheduled_close_at_utc, closed_at_utc, closed_by,
                revision, created_at_utc, updated_at_utc
            ) VALUES (?, '2026-07-01', 'Europe/Kyiv', '23:00', 'closed',
                      'now', 'later', 'done', 20, 1, 'now', 'now')
            """,
            (other_source.id,),
        )
        forged_report = connection.execute(
            """
            INSERT INTO warehouse_reports (
                shift_id, version, report_kind, status, relative_file_path,
                file_sha256, file_size_bytes, created_at_utc, created_by,
                ready_at_utc
            ) VALUES (?, 1, 'final', 'ready', 'reports/warehouse/forged.xlsx',
                      ?, 1, 'now', 20, 'now')
            """,
            (forged_shift.lastrowid, "f" * 64),
        )
        connection.commit()
    first, total = reports.history(actor_user_id=30, page=0)
    second, _ = reports.history(actor_user_id=30, page=1)
    assert len(first) == 8 and len(second) == 1 and total == 9
    with pytest.raises(WarehouseReportsError, match="not found"):
        reports.report_card(int(forged_report.lastrowid), actor_user_id=30)
