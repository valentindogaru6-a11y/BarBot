"""Transactional persistence for immutable warehouse report snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.utils.warehouse_reports import aggregate_report_lines


class WarehouseReportsError(RuntimeError):
    """Raised when a report lifecycle invariant cannot be satisfied."""


class EmptyWarehouseReportError(WarehouseReportsError):
    """Raised until an empty final report is explicitly confirmed."""


class WarehouseReportConflict(WarehouseReportsError):
    """Raised for conflicting concurrent report state."""


class WarehouseReportsDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @staticmethod
    def _dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        shift_id: int,
        actor_user_id: int,
        event_type: str,
        entity_id: int,
        occurred_at_utc: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        correlation_id: str,
    ) -> None:
        encode = lambda value: (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, before_json, after_json,
                reason_text, source_raw_text, correlation_id
            ) VALUES (?, NULL, 'user', ?, ?, 'warehouse_report', ?, ?, ?, ?,
                      NULL, NULL, ?)
            """,
            (
                shift_id,
                actor_user_id,
                event_type,
                entity_id,
                occurred_at_utc,
                encode(before),
                encode(after),
                correlation_id,
            ),
        )

    @classmethod
    def _load_report(
        cls,
        connection: sqlite3.Connection,
        report_id: int,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT r.*, s.business_date, s.status AS shift_status,
                   s.source_location_id
            FROM warehouse_reports r
            JOIN warehouse_shifts s ON s.id=r.shift_id
            WHERE r.id=?
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        report = cls._dict(row)
        lines = connection.execute(
            """
            SELECT * FROM warehouse_report_lines
            WHERE report_id=? ORDER BY sheet_type, row_number
            """,
            (report_id,),
        ).fetchall()
        report["lines"] = [cls._dict(line) for line in lines]
        report["writeoff_count"] = sum(
            line["sheet_type"] == "writeoff" for line in lines
        )
        report["transfer_count"] = sum(
            line["sheet_type"] == "transfer" for line in lines
        )
        report["development_count"] = sum(
            line["sheet_type"] == "development" for line in lines
        )
        report["development_group_count"] = len(
            {
                int(line["development_group_id"])
                for line in lines
                if line["sheet_type"] == "development"
            }
        )
        return report

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._load_report(connection, report_id)

    def report_for_shift(self, shift_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM warehouse_reports
                WHERE shift_id=? AND report_kind='final'
                ORDER BY version DESC LIMIT 1
                """,
                (shift_id,),
            ).fetchone()
            return self._load_report(connection, int(row["id"])) if row else None

    def prepare_initial_report(
        self,
        shift_id: int,
        *,
        actor_user_id: int,
        now: str,
        allow_empty: bool,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        """Create or recover one initial report and its immutable snapshot."""

        with self._transaction() as connection:
            authorize()
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (shift_id,),
            ).fetchone()
            if shift is None:
                raise WarehouseReportsError("Shift not found")
            existing = connection.execute(
                """
                SELECT id, status FROM warehouse_reports
                WHERE shift_id=? AND report_kind='final'
                ORDER BY version DESC LIMIT 1
                """,
                (shift_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "failed":
                    connection.execute(
                        """
                        UPDATE warehouse_reports
                        SET status='building', failure_code=NULL
                        WHERE id=? AND status='failed'
                        """,
                        (existing["id"],),
                    )
                report = self._load_report(connection, int(existing["id"]))
                assert report is not None
                return report
            if shift["status"] != "closing":
                raise WarehouseReportsError("Shift is not ready for final report")
            WarehouseClosingDatabase._recalculate(connection, shift_id, now)
            pending = connection.execute(
                """
                SELECT 1 FROM warehouse_shift_requirements
                WHERE shift_id=? AND status='pending' LIMIT 1
                """,
                (shift_id,),
            ).fetchone()
            if pending is not None:
                raise WarehouseReportsError("Required positions remain pending")

            operation_rows = connection.execute(
                """
                SELECT o.*, src.name AS source_location_name,
                       dst.name AS destination_location_name
                FROM warehouse_operations o
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst
                    ON dst.id=o.destination_location_id
                WHERE o.shift_id=? AND o.is_deleted=0
                ORDER BY o.occurred_at_utc, o.id
                """,
                (shift_id,),
            ).fetchall()
            lines = aggregate_report_lines(
                (self._dict(row) for row in operation_rows),
                business_date=str(shift["business_date"]),
            )
            if not lines and not allow_empty:
                raise EmptyWarehouseReportError("Empty report requires confirmation")
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 "
                    "FROM warehouse_reports WHERE shift_id=?",
                    (shift_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO warehouse_reports (
                    shift_id, version, report_kind, status, relative_file_path,
                    file_sha256, file_size_bytes, created_at_utc, created_by,
                    ready_at_utc, failure_code, supersedes_report_id,
                    correction_reason
                ) VALUES (?, ?, 'final', 'building', NULL, NULL, NULL, ?, ?,
                          NULL, NULL, NULL, NULL)
                """,
                (shift_id, version, now, actor_user_id),
            )
            report_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO warehouse_report_lines (
                    report_id, sheet_type, row_number, product_id, product_name,
                    quantity_scaled, base_unit, reason_or_comment,
                    source_location_name, destination_location_name,
                    development_title, development_group_id, business_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        report_id,
                        line["sheet_type"],
                        line["row_number"],
                        line["product_id"],
                        line["product_name"],
                        line["quantity_scaled"],
                        line["base_unit"],
                        line["reason_or_comment"],
                        line["source_location_name"],
                        line["destination_location_name"],
                        line.get("development_title"),
                        line.get("development_group_id"),
                        line["business_date"],
                    )
                    for line in lines
                ],
            )
            self._audit(
                connection,
                shift_id=shift_id,
                actor_user_id=actor_user_id,
                event_type="warehouse_report_snapshot_created",
                entity_id=report_id,
                occurred_at_utc=now,
                before=None,
                after={"status": "building", "version": version},
                correlation_id=f"report:{report_id}",
            )
            report = self._load_report(connection, report_id)
            assert report is not None
            return report

    def mark_failed(
        self,
        report_id: int,
        *,
        failure_code: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE warehouse_reports
                SET status='failed', failure_code=?
                WHERE id=? AND status='building'
                """,
                (failure_code, report_id),
            )
            if cursor.rowcount == 0:
                current = self._load_report(connection, report_id)
                if current is None:
                    raise WarehouseReportsError("Report not found")
                if current["status"] != "ready":
                    raise WarehouseReportConflict("Report state changed")
            report = self._load_report(connection, report_id)
            assert report is not None
            return report

    def complete_report(
        self,
        report_id: int,
        *,
        relative_file_path: str,
        file_sha256: str,
        file_size_bytes: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        """Atomically mark XLSX ready and transition closing to closed."""

        with self._transaction() as connection:
            authorize()
            report = connection.execute(
                "SELECT * FROM warehouse_reports WHERE id=?",
                (report_id,),
            ).fetchone()
            if report is None:
                raise WarehouseReportsError("Report not found")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (report["shift_id"],),
            ).fetchone()
            if shift is None:
                raise WarehouseReportsError("Shift not found")
            if report["status"] == "ready":
                if (
                    report["relative_file_path"] != relative_file_path
                    or report["file_sha256"] != file_sha256
                    or int(report["file_size_bytes"]) != file_size_bytes
                ):
                    raise WarehouseReportConflict("Ready report metadata mismatch")
            elif report["status"] == "building":
                cursor = connection.execute(
                    """
                    UPDATE warehouse_reports SET
                        status='ready', relative_file_path=?, file_sha256=?,
                        file_size_bytes=?, ready_at_utc=?, failure_code=NULL
                    WHERE id=? AND status='building'
                    """,
                    (
                        relative_file_path,
                        file_sha256,
                        file_size_bytes,
                        now,
                        report_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WarehouseReportConflict("Report state changed")
            else:
                raise WarehouseReportsError("Report is not buildable")

            if shift["status"] == "closing":
                cursor = connection.execute(
                    """
                    UPDATE warehouse_shifts
                    SET status='closed', closed_at_utc=?, closed_by=?,
                        updated_at_utc=?, revision=revision+1
                    WHERE id=? AND status='closing' AND revision=?
                    """,
                    (
                        now,
                        actor_user_id,
                        now,
                        shift["id"],
                        shift["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise WarehouseReportConflict("Shift revision conflict")
                self._audit(
                    connection,
                    shift_id=int(shift["id"]),
                    actor_user_id=actor_user_id,
                    event_type="warehouse_report_ready",
                    entity_id=report_id,
                    occurred_at_utc=now,
                    before={"shift_status": "closing"},
                    after={
                        "shift_status": "closed",
                        "report_status": "ready",
                        "version": int(report["version"]),
                    },
                    correlation_id=f"report:{report_id}",
                )
            elif shift["status"] != "closed":
                raise WarehouseReportsError("Shift cannot be closed")
            completed = self._load_report(connection, report_id)
            assert completed is not None
            return completed

    def ready_history(
        self,
        source_location_id: int,
        *,
        page: int,
        page_size: int = 8,
    ) -> tuple[list[dict[str, Any]], int]:
        offset = max(page, 0) * page_size
        with self._connection() as connection:
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM warehouse_reports r
                    JOIN warehouse_shifts s ON s.id=r.shift_id
                    WHERE r.status='ready' AND s.source_location_id=?
                    """,
                    (source_location_id,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT r.*, s.business_date, s.source_location_id,
                       (SELECT COUNT(*) FROM warehouse_report_lines l
                        WHERE l.report_id=r.id AND l.sheet_type='writeoff')
                            AS writeoff_count,
                       (SELECT COUNT(*) FROM warehouse_report_lines l
                        WHERE l.report_id=r.id AND l.sheet_type='transfer')
                            AS transfer_count,
                       (SELECT COUNT(*) FROM warehouse_report_lines l
                        WHERE l.report_id=r.id AND l.sheet_type='development')
                            AS development_count,
                       (SELECT COUNT(DISTINCT l.development_group_id)
                        FROM warehouse_report_lines l
                        WHERE l.report_id=r.id AND l.sheet_type='development')
                            AS development_group_count
                FROM warehouse_reports r
                JOIN warehouse_shifts s ON s.id=r.shift_id
                WHERE r.status='ready' AND s.source_location_id=?
                ORDER BY s.business_date DESC, r.version DESC
                LIMIT ? OFFSET ?
                """,
                (source_location_id, page_size, offset),
            ).fetchall()
            return [self._dict(row) for row in rows], total

    def live_aggregated_lines(
        self,
        shift_id: int,
        *,
        business_date: str,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT o.*, src.name AS source_location_name,
                       dst.name AS destination_location_name
                FROM warehouse_operations o
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst
                    ON dst.id=o.destination_location_id
                WHERE o.shift_id=? AND o.is_deleted=0
                ORDER BY o.occurred_at_utc, o.id
                """,
                (shift_id,),
            ).fetchall()
            return aggregate_report_lines(
                (self._dict(row) for row in rows),
                business_date=business_date,
            )
