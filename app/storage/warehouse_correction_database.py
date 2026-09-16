"""Transactional persistence for closed-shift correction sessions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.utils.warehouse_reports import aggregate_report_lines


class WarehouseCorrectionError(RuntimeError):
    pass


class WarehouseCorrectionConflict(WarehouseCorrectionError):
    pass


class WarehouseCorrectionDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
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
        session_id: int,
        shift_id: int,
        actor_user_id: int,
        event_type: str,
        entity_type: str,
        entity_id: int,
        now: str,
        operation_id: int | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        encode = lambda value: (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value is not None else None
        )
        connection.execute(
            """
            INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, before_json, after_json,
                reason_text, source_raw_text, correlation_id
            ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                shift_id, operation_id, actor_user_id, event_type, entity_type,
                entity_id, now, encode(before), encode(after), reason,
                f"correction:{session_id}",
            ),
        )

    @classmethod
    def _session(
        cls, connection: sqlite3.Connection, session_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT cs.*, s.status AS shift_status, s.revision AS shift_revision,
                   s.business_date, br.version AS base_version,
                   rr.version AS result_version
            FROM warehouse_correction_sessions cs
            JOIN warehouse_shifts s ON s.id=cs.shift_id
            JOIN warehouse_reports br ON br.id=cs.base_report_id
            LEFT JOIN warehouse_reports rr ON rr.id=cs.result_report_id
            WHERE cs.id=?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        result = cls._dict(row)
        linked = connection.execute(
            "SELECT id FROM warehouse_reports WHERE correction_session_id=?",
            (session_id,),
        ).fetchone()
        result["building_report_id"] = int(linked["id"]) if linked else None
        return result

    def session(self, session_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._session(connection, session_id)

    def report(self, report_id: int) -> dict[str, Any] | None:
        return WarehouseReportsDatabase(self.path).get_report(report_id)

    def history(self, shift_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM warehouse_correction_sessions "
                "WHERE shift_id=? ORDER BY id",
                (shift_id,),
            ).fetchall()
            return [self._session(connection, int(row["id"])) for row in rows]

    def open(
        self,
        shift_id: int,
        reason: str,
        *,
        expected_shift_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            active = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE shift_id=? "
                "AND status IN ('open','building') ORDER BY id LIMIT 1",
                (shift_id,),
            ).fetchone()
            if active is not None:
                if str(active["reason_text"]) == reason:
                    result = self._session(connection, int(active["id"]))
                    assert result is not None
                    return result
                raise WarehouseCorrectionConflict("Active correction already exists")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?", (shift_id,)
            ).fetchone()
            if shift is None or shift["status"] != "closed":
                raise WarehouseCorrectionError("Shift must be closed")
            if int(shift["revision"]) != expected_shift_revision:
                raise WarehouseCorrectionConflict("Shift revision conflict")
            base = connection.execute(
                "SELECT * FROM warehouse_reports WHERE shift_id=? "
                "AND status='ready' ORDER BY version DESC LIMIT 1",
                (shift_id,),
            ).fetchone()
            if base is None:
                raise WarehouseCorrectionError("Ready base report is required")
            cursor = connection.execute(
                """
                INSERT INTO warehouse_correction_sessions (
                    shift_id, base_report_id, result_report_id, status,
                    reason_text, opened_by, opened_at_utc, completed_by,
                    completed_at_utc, revision, created_at_utc, updated_at_utc
                ) VALUES (?, ?, NULL, 'open', ?, ?, ?, NULL, NULL, 1, ?, ?)
                """,
                (shift_id, int(base["id"]), reason, actor_user_id, now, now, now),
            )
            session_id = int(cursor.lastrowid)
            changed = connection.execute(
                "UPDATE warehouse_shifts SET status='correction_open', "
                "updated_at_utc=?, revision=revision+1 "
                "WHERE id=? AND status='closed' AND revision=?",
                (now, shift_id, expected_shift_revision),
            )
            if changed.rowcount != 1:
                raise WarehouseCorrectionConflict("Shift revision conflict")
            self._audit(
                connection, session_id=session_id, shift_id=shift_id,
                actor_user_id=actor_user_id, event_type="correction_opened",
                entity_type="correction_session", entity_id=session_id, now=now,
                before={"shift_status": "closed"},
                after={"shift_status": "correction_open", "base_report_id": int(base["id"])},
                reason=reason,
            )
            result = self._session(connection, session_id)
            assert result is not None
            return result

    @staticmethod
    def _active(
        connection: sqlite3.Connection,
        session_id: int,
        *,
        operation_id: int | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row | None]:
        session = connection.execute(
            """
            SELECT cs.*, s.status AS shift_status
            FROM warehouse_correction_sessions cs
            JOIN warehouse_shifts s ON s.id=cs.shift_id
            WHERE cs.id=?
            """,
            (session_id,),
        ).fetchone()
        if session is None or session["status"] != "open" or session["shift_status"] != "correction_open":
            raise WarehouseCorrectionError("Correction session is not editable")
        operation = None
        if operation_id is not None:
            operation = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=? AND shift_id=?",
                (operation_id, session["shift_id"]),
            ).fetchone()
            if operation is None:
                raise WarehouseCorrectionError("Operation not found")
        return session, operation

    @staticmethod
    def _validate_product(
        connection: sqlite3.Connection,
        product_id: int,
        base_unit: str,
        operation_type: str,
    ) -> sqlite3.Row:
        required = "product_writeoff" if operation_type == "writeoff" else operation_type
        row = connection.execute(
            """
            SELECT p.* FROM warehouse_products p
            JOIN warehouse_product_operation_types t ON t.product_id=p.id
            WHERE p.id=? AND p.is_active=1 AND p.base_unit=?
              AND t.operation_type=?
            """,
            (product_id, base_unit, required),
        ).fetchone()
        if row is None:
            raise WarehouseCorrectionError("Invalid operation product")
        return row

    @staticmethod
    def _recalculate(connection: sqlite3.Connection, shift_id: int, now: str) -> None:
        WarehouseClosingDatabase._recalculate(connection, shift_id, now)

    def create_operation(
        self,
        session_id: int,
        payload: dict[str, Any],
        *,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session, _ = self._active(connection, session_id)
            kind = str(payload["operation_type"])
            product = self._validate_product(
                connection, int(payload["product_id"]), str(payload["base_unit"]), kind
            )
            source_id = int(session["shift_id"])
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?", (source_id,)
            ).fetchone()
            assert shift is not None
            destination = payload.get("destination_location_id")
            if kind == "transfer":
                valid_destination = connection.execute(
                    "SELECT 1 FROM warehouse_locations WHERE id=? AND is_active=1 "
                    "AND can_be_destination=1",
                    (destination,),
                ).fetchone()
                if valid_destination is None or int(destination) == int(shift["source_location_id"]):
                    raise WarehouseCorrectionError("Invalid transfer destination")
            elif destination is not None:
                raise WarehouseCorrectionError("Destination is forbidden")
            development_title = payload.get("development_title")
            development_group_id = None
            if kind == "development":
                if not isinstance(development_title, str) or not development_title.strip() or len(development_title.strip()) > 200:
                    raise WarehouseCorrectionError("Development title is invalid")
                development_title = development_title.strip()
                message_id = int(connection.execute(
                    "SELECT COALESCE(MIN(source_message_id),0)-1 "
                    "FROM warehouse_intake_batches WHERE source_chat_id=0"
                ).fetchone()[0])
                batch = connection.execute(
                    """
                    INSERT INTO warehouse_intake_batches (
                        shift_id, actor_user_id, source_chat_id, source_message_id,
                        operation_type, development_title, raw_text, status,
                        created_at_utc, updated_at_utc, committed_at_utc
                    ) VALUES (?, ?, 0, ?, 'development', ?, ?, 'committed', ?, ?, ?)
                    """,
                    (session["shift_id"], actor_user_id, message_id,
                     development_title, payload["raw_text"], now, now, now),
                )
                development_group_id = int(batch.lastrowid)
            cursor = connection.execute(
                """
                INSERT INTO warehouse_operations (
                    shift_id, operation_type, development_title,
                    development_group_id, product_id, product_name_snapshot,
                    quantity_scaled, base_unit, input_quantity_text,
                    input_unit_text, source_location_id,
                    destination_location_id, reason_text, comment_text, raw_text,
                    author_user_id, occurred_at_utc, intake_line_id, origin,
                    is_deleted, created_at_utc, updated_at_utc, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          NULL, 'manual', 0, ?, ?, 1)
                """,
                (
                    session["shift_id"], kind, development_title,
                    development_group_id, payload["product_id"],
                    product["official_name"], payload["quantity_scaled"],
                    payload["base_unit"], payload["input_quantity_text"],
                    payload.get("input_unit_text"), shift["source_location_id"],
                    destination, payload.get("reason_text"),
                    payload.get("comment_text"), payload["raw_text"],
                    actor_user_id, now, now, now,
                ),
            )
            operation_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert row is not None
            result = self._dict(row)
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id,
                event_type="correction_operation_created",
                entity_type="operation", entity_id=operation_id,
                operation_id=operation_id, now=now, after=result,
            )
            self._recalculate(connection, int(session["shift_id"]), now)
            return result

    def update_operation(
        self,
        session_id: int,
        operation_id: int,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "product_id", "product_name_snapshot", "quantity_scaled", "base_unit",
            "input_quantity_text", "input_unit_text", "reason_text", "comment_text",
            "destination_location_id",
        }
        if not changes or not set(changes) <= allowed:
            raise WarehouseCorrectionError("Invalid operation changes")
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session, current = self._active(
                connection, session_id, operation_id=operation_id
            )
            assert current is not None
            if int(current["revision"]) != expected_revision:
                raise WarehouseCorrectionConflict("Operation revision conflict")
            if current["is_deleted"]:
                raise WarehouseCorrectionError("Deleted operation must be restored first")
            proposed = self._dict(current)
            proposed.update(changes)
            self._validate_product(
                connection, int(proposed["product_id"]),
                str(proposed["base_unit"]), str(proposed["operation_type"]),
            )
            destination = proposed["destination_location_id"]
            if proposed["operation_type"] == "transfer":
                valid = connection.execute(
                    "SELECT 1 FROM warehouse_locations WHERE id=? AND is_active=1 "
                    "AND can_be_destination=1",
                    (destination,),
                ).fetchone()
                if valid is None or int(destination) == int(proposed["source_location_id"]):
                    raise WarehouseCorrectionError("Invalid transfer destination")
            elif destination is not None:
                raise WarehouseCorrectionError("Destination is forbidden")
            assignments = ", ".join(f"{key}=?" for key in changes)
            changed = connection.execute(
                f"UPDATE warehouse_operations SET {assignments}, "
                "updated_at_utc=?, revision=revision+1 WHERE id=? AND revision=?",
                (*changes.values(), now, operation_id, expected_revision),
            )
            if changed.rowcount != 1:
                raise WarehouseCorrectionConflict("Operation revision conflict")
            updated_row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert updated_row is not None
            before = self._dict(current); updated = self._dict(updated_row)
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id,
                event_type="correction_operation_updated",
                entity_type="operation", entity_id=operation_id,
                operation_id=operation_id, now=now, before=before, after=updated,
            )
            self._recalculate(connection, int(session["shift_id"]), now)
            return updated

    def set_deleted(
        self,
        session_id: int,
        operation_id: int,
        deleted: bool,
        *,
        expected_revision: int,
        actor_user_id: int,
        reason: str,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session, current = self._active(
                connection, session_id, operation_id=operation_id
            )
            assert current is not None
            if int(current["revision"]) != expected_revision:
                raise WarehouseCorrectionConflict("Operation revision conflict")
            if bool(current["is_deleted"]) == deleted:
                raise WarehouseCorrectionError("Operation deletion state is unchanged")
            if deleted:
                connection.execute(
                    "UPDATE warehouse_operations SET is_deleted=1, deleted_at_utc=?, "
                    "deleted_by=?, deletion_reason=?, updated_at_utc=?, "
                    "revision=revision+1 WHERE id=?",
                    (now, actor_user_id, reason, now, operation_id),
                )
                event = "correction_operation_deleted"
            else:
                connection.execute(
                    "UPDATE warehouse_operations SET is_deleted=0, deleted_at_utc=NULL, "
                    "deleted_by=NULL, deletion_reason=NULL, updated_at_utc=?, "
                    "revision=revision+1 WHERE id=?",
                    (now, operation_id),
                )
                event = "correction_operation_restored"
            updated_row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert updated_row is not None
            before = self._dict(current); updated = self._dict(updated_row)
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id, event_type=event,
                entity_type="operation", entity_id=operation_id,
                operation_id=operation_id, now=now, before=before, after=updated,
                reason=reason,
            )
            self._recalculate(connection, int(session["shift_id"]), now)
            return updated

    def rename_development(
        self,
        session_id: int,
        operation_id: int,
        title: str,
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session, current = self._active(
                connection, session_id, operation_id=operation_id
            )
            assert current is not None
            if current["operation_type"] != "development":
                raise WarehouseCorrectionError("Development operation not found")
            if int(current["revision"]) != expected_revision:
                raise WarehouseCorrectionConflict("Operation revision conflict")
            if current["is_deleted"]:
                raise WarehouseCorrectionError("Deleted operation must be restored first")
            group_id = int(current["development_group_id"])
            if connection.execute(
                "SELECT 1 FROM warehouse_operations WHERE development_group_id=? "
                "AND is_deleted=1 LIMIT 1", (group_id,)
            ).fetchone():
                raise WarehouseCorrectionError("Restore deleted group operations first")
            before = str(current["development_title"])
            connection.execute(
                "UPDATE warehouse_operations SET development_title=?, "
                "updated_at_utc=?, revision=revision+1 "
                "WHERE development_group_id=? AND operation_type='development'",
                (title, now, group_id),
            )
            connection.execute(
                "UPDATE warehouse_intake_batches SET development_title=?, "
                "updated_at_utc=? WHERE id=?",
                (title, now, group_id),
            )
            updated = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert updated is not None
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id,
                event_type="correction_development_group_renamed",
                entity_type="development_group", entity_id=group_id,
                operation_id=operation_id, now=now,
                before={"development_title": before},
                after={"development_title": title},
            )
            return self._dict(updated)

    def cancel(
        self,
        session_id: int,
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseCorrectionError("Correction session not found")
            if session["status"] == "cancelled":
                result = self._session(connection, session_id); assert result is not None
                return result
            if session["status"] != "open":
                raise WarehouseCorrectionError("Correction cannot be cancelled")
            if int(session["revision"]) != expected_revision:
                raise WarehouseCorrectionConflict("Correction revision conflict")
            mutated = connection.execute(
                "SELECT 1 FROM warehouse_audit_events WHERE correlation_id=? "
                "AND event_type LIKE 'correction_%' "
                "AND event_type NOT IN ('correction_opened','correction_cancelled') LIMIT 1",
                (f"correction:{session_id}",),
            ).fetchone()
            if mutated is not None:
                raise WarehouseCorrectionError("Correction with mutations cannot be cancelled")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?", (session["shift_id"],)
            ).fetchone()
            if shift is None or shift["status"] != "correction_open":
                raise WarehouseCorrectionError("Shift is not in correction")
            connection.execute(
                "UPDATE warehouse_correction_sessions SET status='cancelled', "
                "completed_by=?, completed_at_utc=?, updated_at_utc=?, "
                "revision=revision+1 WHERE id=? AND status='open' AND revision=?",
                (actor_user_id, now, now, session_id, expected_revision),
            )
            connection.execute(
                "UPDATE warehouse_shifts SET status='closed', updated_at_utc=?, "
                "revision=revision+1 WHERE id=? AND status='correction_open'",
                (now, session["shift_id"]),
            )
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id, event_type="correction_cancelled",
                entity_type="correction_session", entity_id=session_id, now=now,
                before={"status": "open"}, after={"status": "cancelled"},
            )
            result = self._session(connection, session_id); assert result is not None
            return result

    def begin_finalize(
        self,
        session_id: int,
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseCorrectionError("Correction session not found")
            if session["status"] == "completed":
                report = WarehouseReportsDatabase._load_report(
                    connection, int(session["result_report_id"])
                )
                return {"ready": True, "session": self._dict(session), "report": report, "missing": []}
            linked = connection.execute(
                "SELECT id FROM warehouse_reports WHERE correction_session_id=?",
                (session_id,),
            ).fetchone()
            if session["status"] in ("building", "failed") and linked is not None:
                if session["status"] == "failed":
                    connection.execute(
                        "UPDATE warehouse_correction_sessions SET status='building', "
                        "updated_at_utc=?, revision=revision+1 WHERE id=? AND status='failed'",
                        (now, session_id),
                    )
                report = WarehouseReportsDatabase._load_report(connection, int(linked["id"]))
                refreshed = self._session(connection, session_id)
                return {"ready": True, "session": refreshed, "report": report, "missing": []}
            if session["status"] != "open":
                raise WarehouseCorrectionError("Correction cannot be finalized")
            if int(session["revision"]) != expected_revision:
                raise WarehouseCorrectionConflict("Correction revision conflict")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?", (session["shift_id"],)
            ).fetchone()
            if shift is None or shift["status"] != "correction_open":
                raise WarehouseCorrectionError("Shift is not in correction")
            self._recalculate(connection, int(session["shift_id"]), now)
            missing_rows = connection.execute(
                """
                SELECT sr.id, sr.product_id, p.official_name AS product_name
                FROM warehouse_shift_requirements sr
                JOIN warehouse_products p ON p.id=sr.product_id
                WHERE sr.shift_id=? AND sr.status='pending'
                ORDER BY sr.sort_order, sr.id
                """,
                (session["shift_id"],),
            ).fetchall()
            if missing_rows:
                refreshed = self._session(connection, session_id)
                return {
                    "ready": False, "session": refreshed,
                    "report": None,
                    "missing": [self._dict(row) for row in missing_rows],
                }
            operations = connection.execute(
                """
                SELECT o.*, src.name AS source_location_name,
                       dst.name AS destination_location_name
                FROM warehouse_operations o
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst ON dst.id=o.destination_location_id
                WHERE o.shift_id=? AND o.is_deleted=0
                ORDER BY o.occurred_at_utc, o.id
                """,
                (session["shift_id"],),
            ).fetchall()
            lines = aggregate_report_lines(
                (self._dict(row) for row in operations),
                business_date=str(shift["business_date"]),
            )
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM warehouse_reports WHERE shift_id=?",
                (session["shift_id"],),
            ).fetchone()[0])
            cursor = connection.execute(
                """
                INSERT INTO warehouse_reports (
                    shift_id, version, report_kind, status, relative_file_path,
                    file_sha256, file_size_bytes, created_at_utc, created_by,
                    ready_at_utc, failure_code, supersedes_report_id,
                    correction_reason, correction_session_id
                ) VALUES (?, ?, 'final', 'building', NULL, NULL, NULL, ?, ?,
                          NULL, NULL, ?, ?, ?)
                """,
                (
                    session["shift_id"], version, now, actor_user_id,
                    session["base_report_id"], session["reason_text"], session_id,
                ),
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
                [(
                    report_id, line["sheet_type"], line["row_number"],
                    line["product_id"], line["product_name"],
                    line["quantity_scaled"], line["base_unit"],
                    line["reason_or_comment"], line["source_location_name"],
                    line["destination_location_name"],
                    line.get("development_title"), line.get("development_group_id"),
                    line["business_date"],
                ) for line in lines],
            )
            changed = connection.execute(
                "UPDATE warehouse_correction_sessions SET status='building', "
                "updated_at_utc=?, revision=revision+1 "
                "WHERE id=? AND status='open' AND revision=?",
                (now, session_id, expected_revision),
            )
            if changed.rowcount != 1:
                raise WarehouseCorrectionConflict("Correction revision conflict")
            shifted = connection.execute(
                "UPDATE warehouse_shifts SET status='closing', updated_at_utc=?, "
                "revision=revision+1 WHERE id=? AND status='correction_open' "
                "AND revision=?",
                (now, session["shift_id"], shift["revision"]),
            )
            if shifted.rowcount != 1:
                raise WarehouseCorrectionConflict("Shift revision conflict")
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id,
                event_type="correction_report_snapshot_created",
                entity_type="warehouse_report", entity_id=report_id, now=now,
                after={"status": "building", "version": version},
            )
            report = WarehouseReportsDatabase._load_report(connection, report_id)
            refreshed = self._session(connection, session_id)
            return {"ready": True, "session": refreshed, "report": report, "missing": []}

    def mark_build_failed(
        self, session_id: int, *, actor_user_id: int, now: str
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseCorrectionError("Correction session not found")
            if session["status"] == "building":
                connection.execute(
                    "UPDATE warehouse_correction_sessions SET status='failed', "
                    "updated_at_utc=?, revision=revision+1 WHERE id=? AND status='building'",
                    (now, session_id),
                )
                self._audit(
                    connection, session_id=session_id,
                    shift_id=int(session["shift_id"]), actor_user_id=actor_user_id,
                    event_type="correction_build_failed",
                    entity_type="correction_session", entity_id=session_id, now=now,
                    before={"status": "building"}, after={"status": "failed"},
                )
            result = self._session(connection, session_id); assert result is not None
            return result

    def complete(
        self,
        session_id: int,
        report_id: int,
        *,
        relative_file_path: str,
        file_sha256: str,
        file_size_bytes: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            session = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseCorrectionError("Correction session not found")
            if session["status"] == "completed":
                if int(session["result_report_id"]) != report_id:
                    raise WarehouseCorrectionConflict("Correction result changed")
                result = self._session(connection, session_id); assert result is not None
                return result
            report = connection.execute(
                "SELECT * FROM warehouse_reports WHERE id=? AND correction_session_id=?",
                (report_id, session_id),
            ).fetchone()
            if report is None:
                raise WarehouseCorrectionError("Correction report not found")
            if report["status"] == "building":
                changed = connection.execute(
                    """
                    UPDATE warehouse_reports SET status='ready',
                        relative_file_path=?, file_sha256=?, file_size_bytes=?,
                        ready_at_utc=?, failure_code=NULL
                    WHERE id=? AND status='building'
                    """,
                    (relative_file_path, file_sha256, file_size_bytes, now, report_id),
                )
                if changed.rowcount != 1:
                    raise WarehouseCorrectionConflict("Report state changed")
            elif report["status"] == "ready":
                if (
                    report["relative_file_path"] != relative_file_path
                    or report["file_sha256"] != file_sha256
                    or int(report["file_size_bytes"]) != file_size_bytes
                ):
                    raise WarehouseCorrectionConflict("Ready report metadata mismatch")
            else:
                raise WarehouseCorrectionError("Correction report is not buildable")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?", (session["shift_id"],)
            ).fetchone()
            if shift is None or shift["status"] not in ("closing", "closed"):
                raise WarehouseCorrectionError("Shift cannot finish correction")
            if shift["status"] == "closing":
                connection.execute(
                    "UPDATE warehouse_shifts SET status='closed', updated_at_utc=?, "
                    "revision=revision+1 WHERE id=? AND status='closing'",
                    (now, session["shift_id"]),
                )
            connection.execute(
                "UPDATE warehouse_correction_sessions SET status='completed', "
                "result_report_id=?, completed_by=?, completed_at_utc=?, "
                "updated_at_utc=?, revision=revision+1 "
                "WHERE id=? AND status IN ('building','failed')",
                (report_id, actor_user_id, now, now, session_id),
            )
            self._audit(
                connection, session_id=session_id, shift_id=int(session["shift_id"]),
                actor_user_id=actor_user_id,
                event_type="correction_completed",
                entity_type="correction_session", entity_id=session_id, now=now,
                before={"status": session["status"]},
                after={"status": "completed", "result_report_id": report_id,
                       "version": int(report["version"])},
            )
            result = self._session(connection, session_id); assert result is not None
            return result

    def delivery_recipient_ids(self, session_id: int) -> tuple[int, ...]:
        with self._connection() as connection:
            session = connection.execute(
                "SELECT * FROM warehouse_correction_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None or session["status"] != "completed":
                raise WarehouseCorrectionError("Correction report is not ready")
            rows = connection.execute(
                """
                SELECT r.id FROM warehouse_report_deliveries d
                JOIN warehouse_report_recipients r ON r.id=d.recipient_id
                WHERE d.report_id=? AND d.status='delivered' AND r.is_active=1
                ORDER BY r.title COLLATE NOCASE, r.id
                """,
                (session["base_report_id"],),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    "SELECT id FROM warehouse_report_recipients WHERE is_active=1 "
                    "ORDER BY title COLLATE NOCASE,id"
                ).fetchall()
            return tuple(int(row["id"]) for row in rows)

    def delivery_preview(self, session_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            session = self._session(connection, session_id)
            if session is None or session["status"] != "completed":
                raise WarehouseCorrectionError("Correction report is not ready")
            ids = self.delivery_recipient_ids(session_id)
            if ids:
                recipients = [self._dict(row) for row in connection.execute(
                    "SELECT id,title,chat_type FROM warehouse_report_recipients "
                    "WHERE id IN (" + ",".join("?" for _ in ids) + ") "
                    "ORDER BY title COLLATE NOCASE,id", ids
                ).fetchall()]
            else:
                recipients = []
            return {
                "session_id": session_id,
                "report_id": int(session["result_report_id"]),
                "version": int(session["result_version"]),
                "reason": str(session["reason_text"]),
                "recipient_ids": ids,
                "recipients": recipients,
            }
