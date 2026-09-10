"""Transactional SQLite repository for required write-offs and shift closing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator


class WarehouseClosingError(RuntimeError):
    """Raised when a closing invariant cannot be satisfied."""


class WarehouseClosingConflict(WarehouseClosingError):
    """Raised for a stale rule, session, or shift revision."""


class WarehouseClosingDatabase:
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
        shift_id: int | None,
        operation_id: int | None,
        actor_user_id: int,
        event_type: str,
        entity_type: str,
        entity_id: int,
        occurred_at_utc: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason_text: str | None = None,
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
            ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                shift_id,
                operation_id,
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                occurred_at_utc,
                encode(before),
                encode(after),
                reason_text,
                correlation_id,
            ),
        )

    def list_rules(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        with self._connection() as connection:
            where = "" if include_inactive else "WHERE r.is_active=1"
            rows = connection.execute(
                f"""
                SELECT r.*, p.official_name AS product_name
                FROM warehouse_required_rules r
                JOIN warehouse_products p ON p.id=r.product_id
                {where}
                ORDER BY r.is_active DESC, r.sort_order, r.id
                """
            ).fetchall()
            return [self._dict(row) for row in rows]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.*, p.official_name AS product_name
                FROM warehouse_required_rules r
                JOIN warehouse_products p ON p.id=r.product_id
                WHERE r.id=?
                """,
                (rule_id,),
            ).fetchone()
            return self._dict(row) if row else None

    def rule_stable_ids(self) -> set[str]:
        with self._connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT stable_id FROM warehouse_required_rules"
                ).fetchall()
            }

    def create_rule(
        self,
        payload: dict[str, Any],
        *,
        stable_id: str,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            product = connection.execute(
                "SELECT id, base_unit FROM warehouse_products "
                "WHERE id=? AND is_active=1",
                (payload["product_id"],),
            ).fetchone()
            if product is None or product["base_unit"] != payload["base_unit"]:
                raise WarehouseClosingError("Active product unit does not match rule")
            cursor = connection.execute(
                """
                INSERT INTO warehouse_required_rules (
                    stable_id, product_id, operation_type, quantity_mode,
                    suggested_quantity_scaled, base_unit, weekdays_mask,
                    question_text, sort_order, allow_not_applicable,
                    minimum_scaled, maximum_scaled, is_active, created_at_utc,
                    created_by, updated_at_utc, updated_by, revision
                ) VALUES (?, ?, 'writeoff', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    stable_id,
                    payload["product_id"],
                    payload["quantity_mode"],
                    payload.get("suggested_quantity_scaled"),
                    payload["base_unit"],
                    payload["weekdays_mask"],
                    payload["question_text"],
                    payload["sort_order"],
                    int(payload["allow_not_applicable"]),
                    payload.get("minimum_scaled"),
                    payload.get("maximum_scaled"),
                    int(payload["is_active"]),
                    now,
                    actor_user_id,
                    now,
                    actor_user_id,
                ),
            )
            rule_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM warehouse_required_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            assert row is not None
            self._audit(
                connection,
                shift_id=None,
                operation_id=None,
                actor_user_id=actor_user_id,
                event_type="required_rule_created",
                entity_type="required_rule",
                entity_id=rule_id,
                occurred_at_utc=now,
                after=self._dict(row),
                correlation_id=f"required-rule:{rule_id}:r1",
            )
        result = self.get_rule(rule_id)
        assert result is not None
        return result

    def update_rule(
        self,
        rule_id: int,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT * FROM warehouse_required_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            if current is None:
                raise WarehouseClosingError("Required rule not found")
            if int(current["revision"]) != expected_revision:
                raise WarehouseClosingConflict("Required rule revision conflict")
            product = connection.execute(
                "SELECT id, base_unit FROM warehouse_products "
                "WHERE id=? AND is_active=1",
                (payload["product_id"],),
            ).fetchone()
            if product is None or product["base_unit"] != payload["base_unit"]:
                raise WarehouseClosingError("Active product unit does not match rule")
            cursor = connection.execute(
                """
                UPDATE warehouse_required_rules SET
                    product_id=?, quantity_mode=?, suggested_quantity_scaled=?,
                    base_unit=?, weekdays_mask=?, question_text=?, sort_order=?,
                    allow_not_applicable=?, minimum_scaled=?, maximum_scaled=?,
                    is_active=?, updated_at_utc=?, updated_by=?, revision=revision+1
                WHERE id=? AND revision=?
                """,
                (
                    payload["product_id"],
                    payload["quantity_mode"],
                    payload.get("suggested_quantity_scaled"),
                    payload["base_unit"],
                    payload["weekdays_mask"],
                    payload["question_text"],
                    payload["sort_order"],
                    int(payload["allow_not_applicable"]),
                    payload.get("minimum_scaled"),
                    payload.get("maximum_scaled"),
                    int(payload["is_active"]),
                    now,
                    actor_user_id,
                    rule_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WarehouseClosingConflict("Required rule revision conflict")
            updated = connection.execute(
                "SELECT * FROM warehouse_required_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            assert updated is not None
            self._audit(
                connection,
                shift_id=None,
                operation_id=None,
                actor_user_id=actor_user_id,
                event_type="required_rule_updated",
                entity_type="required_rule",
                entity_id=rule_id,
                occurred_at_utc=now,
                before=self._dict(current),
                after=self._dict(updated),
                correlation_id=f"required-rule:{rule_id}:r{updated['revision']}",
            )
        result = self.get_rule(rule_id)
        assert result is not None
        return result

    @staticmethod
    def _ensure_snapshot(
        connection: sqlite3.Connection,
        shift: sqlite3.Row,
        now: str,
    ) -> None:
        has_requirements = connection.execute(
            "SELECT 1 FROM warehouse_shift_requirements WHERE shift_id=? LIMIT 1",
            (shift["id"],),
        ).fetchone()
        has_session = connection.execute(
            "SELECT 1 FROM warehouse_closing_sessions WHERE shift_id=? LIMIT 1",
            (shift["id"],),
        ).fetchone()
        if has_requirements is not None or has_session is not None:
            return
        weekday_bit = 1 << date.fromisoformat(str(shift["business_date"])).weekday()
        connection.execute(
            """
            INSERT INTO warehouse_shift_requirements (
                shift_id, rule_id, product_id, operation_type, quantity_mode,
                suggested_quantity_scaled, base_unit, question_text, sort_order,
                allow_not_applicable, minimum_scaled, maximum_scaled, status,
                resolved_at_utc, resolved_by, resolution_reason,
                created_at_utc, updated_at_utc
            )
            SELECT ?, id, product_id, operation_type, quantity_mode,
                   suggested_quantity_scaled, base_unit, question_text, sort_order,
                   allow_not_applicable, minimum_scaled, maximum_scaled, 'pending',
                   NULL, NULL, NULL, ?, ?
            FROM warehouse_required_rules
            WHERE is_active=1 AND (weekdays_mask & ?) <> 0
            ORDER BY sort_order, id
            """,
            (shift["id"], now, now, weekday_bit),
        )

    @staticmethod
    def _recalculate(
        connection: sqlite3.Connection,
        shift_id: int,
        now: str,
    ) -> None:
        requirements = connection.execute(
            "SELECT * FROM warehouse_shift_requirements WHERE shift_id=?",
            (shift_id,),
        ).fetchall()
        for requirement in requirements:
            operation = connection.execute(
                """
                SELECT COALESCE(SUM(quantity_scaled), 0) AS total,
                       MAX(occurred_at_utc) AS last_at,
                       MAX(author_user_id) AS actor
                FROM warehouse_operations
                WHERE shift_id=? AND product_id=? AND operation_type=?
                  AND base_unit=? AND is_deleted=0
                """,
                (
                    shift_id,
                    requirement["product_id"],
                    requirement["operation_type"],
                    requirement["base_unit"],
                ),
            ).fetchone()
            total = int(operation["total"])
            satisfied = total > 0
            if requirement["minimum_scaled"] is not None:
                satisfied = satisfied and total >= int(requirement["minimum_scaled"])
            if requirement["maximum_scaled"] is not None:
                satisfied = satisfied and total <= int(requirement["maximum_scaled"])
            if satisfied:
                connection.execute(
                    """
                    UPDATE warehouse_shift_requirements
                    SET status='satisfied', resolved_at_utc=?, resolved_by=?,
                        resolution_reason=NULL, updated_at_utc=? WHERE id=?
                    """,
                    (
                        operation["last_at"] or now,
                        operation["actor"],
                        now,
                        requirement["id"],
                    ),
                )
            elif requirement["status"] != "not_applicable":
                connection.execute(
                    """
                    UPDATE warehouse_shift_requirements
                    SET status='pending', resolved_at_utc=NULL, resolved_by=NULL,
                        resolution_reason=NULL, updated_at_utc=? WHERE id=?
                    """,
                    (now, requirement["id"]),
                )

    @staticmethod
    def _next_requirement(
        connection: sqlite3.Connection,
        shift_id: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT sr.*, p.official_name AS product_name
            FROM warehouse_shift_requirements sr
            JOIN warehouse_products p ON p.id=sr.product_id
            WHERE sr.shift_id=? AND sr.status='pending'
            ORDER BY sr.sort_order, sr.id LIMIT 1
            """,
            (shift_id,),
        ).fetchone()

    @classmethod
    def _refresh_session(
        cls,
        connection: sqlite3.Connection,
        session_id: int,
        now: str,
    ) -> None:
        session = connection.execute(
            "SELECT * FROM warehouse_closing_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if session is None or session["status"] not in ("collecting", "ready"):
            return
        cls._recalculate(connection, int(session["shift_id"]), now)
        pending = cls._next_requirement(connection, int(session["shift_id"]))
        connection.execute(
            """
            UPDATE warehouse_closing_sessions
            SET status=?, current_requirement_id=?, last_activity_at_utc=?,
                revision=revision+1 WHERE id=?
            """,
            (
                "collecting" if pending else "ready",
                int(pending["id"]) if pending else None,
                now,
                session_id,
            ),
        )

    def ensure_shift(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_shifts "
                "WHERE source_location_id=? AND business_date=?",
                (payload["source_location_id"], payload["business_date"]),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO warehouse_shifts (
                        source_location_id, business_date, timezone_snapshot,
                        cutoff_snapshot, status, started_at_utc,
                        scheduled_close_at_utc, closed_at_utc, closed_by,
                        revision, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, NULL, 1, ?, ?)
                    """,
                    (
                        payload["source_location_id"],
                        payload["business_date"],
                        payload["timezone"],
                        payload["cutoff"],
                        payload["started_at_utc"],
                        payload["scheduled_close_at_utc"],
                        payload["now"],
                        payload["now"],
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM warehouse_shifts WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
            assert row is not None
            return self._dict(row)

    def session(self, session_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT cs.*, s.status AS shift_status, s.business_date,
                       s.revision AS shift_revision
                FROM warehouse_closing_sessions cs
                JOIN warehouse_shifts s ON s.id=cs.shift_id
                WHERE cs.id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._dict(row)
            requirements = connection.execute(
                """
                SELECT sr.*, p.official_name AS product_name
                FROM warehouse_shift_requirements sr
                JOIN warehouse_products p ON p.id=sr.product_id
                WHERE sr.shift_id=? ORDER BY sr.sort_order, sr.id
                """,
                (row["shift_id"],),
            ).fetchall()
            result["requirements"] = [self._dict(item) for item in requirements]
            return result

    def active_session(self, shift_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM warehouse_closing_sessions
                WHERE shift_id=? AND status IN ('collecting','ready','committing')
                ORDER BY id LIMIT 1
                """,
                (shift_id,),
            ).fetchone()
        return self.session(int(row["id"])) if row else None

    def refresh_session(self, session_id: int, *, now: str) -> dict[str, Any]:
        with self._transaction() as connection:
            self._refresh_session(connection, session_id, now)
        result = self.session(session_id)
        if result is None:
            raise WarehouseClosingError("Closing session not found")
        return result

    def prepare_requirements(
        self,
        shift_id: int,
        *,
        now: str,
    ) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (shift_id,),
            ).fetchone()
            if shift is None:
                raise WarehouseClosingError("Shift not found")
            self._ensure_snapshot(connection, shift, now)
            self._recalculate(connection, shift_id, now)
            rows = connection.execute(
                """
                SELECT sr.*, p.official_name AS product_name
                FROM warehouse_shift_requirements sr
                JOIN warehouse_products p ON p.id=sr.product_id
                WHERE sr.shift_id=? ORDER BY sr.sort_order, sr.id
                """,
                (shift_id,),
            ).fetchall()
            return [self._dict(row) for row in rows]

    def start_closing(
        self,
        shift_id: int,
        *,
        actor_user_id: int,
        trigger_type: str,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (shift_id,),
            ).fetchone()
            if shift is None:
                raise WarehouseClosingError("Shift not found")
            if shift["status"] == "closing":
                completed = connection.execute(
                    "SELECT id FROM warehouse_closing_sessions "
                    "WHERE shift_id=? AND status='completed' ORDER BY id DESC LIMIT 1",
                    (shift_id,),
                ).fetchone()
                return self.session(int(completed["id"])) if completed else {
                    "shift_id": shift_id,
                    "shift_status": "closing",
                    "status": "completed",
                    "requirements": [],
                }
            if shift["status"] not in ("open", "waiting_required"):
                raise WarehouseClosingError("Shift cannot start closing")
            self._ensure_snapshot(connection, shift, now)
            self._recalculate(connection, shift_id, now)
            active = connection.execute(
                """
                SELECT id FROM warehouse_closing_sessions
                WHERE shift_id=? AND status IN ('collecting','ready','committing')
                ORDER BY id LIMIT 1
                """,
                (shift_id,),
            ).fetchone()
            if active is None:
                pending = self._next_requirement(connection, shift_id)
                cursor = connection.execute(
                    """
                    INSERT INTO warehouse_closing_sessions (
                        shift_id, initiated_by, trigger_type, status,
                        current_requirement_id, created_at_utc,
                        last_activity_at_utc, completed_at_utc, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
                    """,
                    (
                        shift_id,
                        actor_user_id,
                        trigger_type,
                        "collecting" if pending else "ready",
                        int(pending["id"]) if pending else None,
                        now,
                        now,
                    ),
                )
                session_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    UPDATE warehouse_shifts SET status='waiting_required',
                        updated_at_utc=?, revision=revision+1
                    WHERE id=? AND status='open'
                    """,
                    (now, shift_id),
                )
                self._audit(
                    connection,
                    shift_id=shift_id,
                    operation_id=None,
                    actor_user_id=actor_user_id,
                    event_type="closing_started",
                    entity_type="closing_session",
                    entity_id=session_id,
                    occurred_at_utc=now,
                    after={"trigger_type": trigger_type},
                    correlation_id=f"closing:{session_id}",
                )
            else:
                session_id = int(active["id"])
                self._refresh_session(connection, session_id, now)
        result = self.session(session_id)
        assert result is not None
        return result

    def record_required_operation(
        self,
        session_id: int,
        requirement_id: int,
        *,
        quantity_scaled: int,
        input_quantity_text: str,
        input_unit_text: str | None,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM warehouse_closing_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None or session["status"] not in ("collecting", "ready"):
                raise WarehouseClosingError("Closing session is not active")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (session["shift_id"],),
            ).fetchone()
            requirement = connection.execute(
                """
                SELECT sr.*, p.official_name AS product_name, p.is_active,
                       p.base_unit AS product_base_unit,
                       EXISTS (
                           SELECT 1 FROM warehouse_product_operation_types pot
                           WHERE pot.product_id=p.id
                             AND pot.operation_type='product_writeoff'
                       ) AS supports_writeoff
                FROM warehouse_shift_requirements sr
                JOIN warehouse_products p ON p.id=sr.product_id
                WHERE sr.id=? AND sr.shift_id=?
                """,
                (requirement_id, session["shift_id"]),
            ).fetchone()
            if shift is None or shift["status"] not in ("open", "waiting_required"):
                raise WarehouseClosingError("Shift is not editable")
            if requirement is None:
                raise WarehouseClosingError("Requirement not found")
            self._recalculate(connection, int(shift["id"]), now)
            current = connection.execute(
                "SELECT status FROM warehouse_shift_requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            if current["status"] != "pending":
                self._refresh_session(connection, session_id, now)
                return {"created": False, "operation_id": None}
            if (
                not requirement["is_active"]
                or not requirement["supports_writeoff"]
                or requirement["product_base_unit"] != requirement["base_unit"]
                or quantity_scaled <= 0
            ):
                raise WarehouseClosingError("Required product is unavailable")
            cursor = connection.execute(
                """
                INSERT INTO warehouse_operations (
                    shift_id, operation_type, product_id, product_name_snapshot,
                    quantity_scaled, base_unit, input_quantity_text, input_unit_text,
                    source_location_id, destination_location_id, reason_text,
                    comment_text, raw_text, author_user_id, occurred_at_utc,
                    intake_line_id, origin, is_deleted, created_at_utc,
                    updated_at_utc, revision
                ) VALUES (?, 'writeoff', ?, ?, ?, ?, ?, ?, ?, NULL,
                          'Обязательное списание', NULL, ?, ?, ?, NULL,
                          'required_rule', 0, ?, ?, 1)
                """,
                (
                    shift["id"],
                    requirement["product_id"],
                    requirement["product_name"],
                    quantity_scaled,
                    requirement["base_unit"],
                    input_quantity_text,
                    input_unit_text,
                    shift["source_location_id"],
                    f"required:{requirement_id}:{input_quantity_text}",
                    actor_user_id,
                    now,
                    now,
                    now,
                ),
            )
            operation_id = int(cursor.lastrowid)
            operation = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
            assert operation is not None
            self._audit(
                connection,
                shift_id=int(shift["id"]),
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                event_type="operation_created",
                entity_type="operation",
                entity_id=operation_id,
                occurred_at_utc=now,
                after=self._dict(operation),
                correlation_id=f"closing:{session_id}",
            )
            self._recalculate(connection, int(shift["id"]), now)
            resolved = connection.execute(
                "SELECT status FROM warehouse_shift_requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            if resolved["status"] != "satisfied":
                raise WarehouseClosingError("Quantity does not satisfy requirement limits")
            self._audit(
                connection,
                shift_id=int(shift["id"]),
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                event_type="requirement_satisfied",
                entity_type="shift_requirement",
                entity_id=requirement_id,
                occurred_at_utc=now,
                after={"status": "satisfied"},
                correlation_id=f"closing:{session_id}",
            )
            self._refresh_session(connection, session_id, now)
            return {"created": True, "operation_id": operation_id}

    def mark_not_applicable(
        self,
        session_id: int,
        requirement_id: int,
        *,
        reason: str,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM warehouse_closing_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            requirement = connection.execute(
                "SELECT * FROM warehouse_shift_requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            if session is None or session["status"] not in ("collecting", "ready"):
                raise WarehouseClosingError("Closing session is not active")
            if requirement is None or requirement["shift_id"] != session["shift_id"]:
                raise WarehouseClosingError("Requirement not found")
            if not requirement["allow_not_applicable"]:
                raise WarehouseClosingError("Not-applicable is forbidden")
            self._recalculate(connection, int(session["shift_id"]), now)
            current = connection.execute(
                "SELECT * FROM warehouse_shift_requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            if current["status"] == "satisfied":
                self._refresh_session(connection, session_id, now)
                return self._dict(current)
            if reason == "Уже списано ранее":
                raise WarehouseClosingError("No matching operation exists")
            connection.execute(
                """
                UPDATE warehouse_shift_requirements
                SET status='not_applicable', resolved_at_utc=?, resolved_by=?,
                    resolution_reason=?, updated_at_utc=? WHERE id=?
                """,
                (now, actor_user_id, reason, now, requirement_id),
            )
            self._audit(
                connection,
                shift_id=int(session["shift_id"]),
                operation_id=None,
                actor_user_id=actor_user_id,
                event_type="requirement_not_applicable",
                entity_type="shift_requirement",
                entity_id=requirement_id,
                occurred_at_utc=now,
                after={"status": "not_applicable"},
                reason_text=reason,
                correlation_id=f"closing:{session_id}",
            )
            self._refresh_session(connection, session_id, now)
        result = self.session(session_id)
        assert result is not None
        return result

    def cancel_session(
        self,
        session_id: int,
        *,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM warehouse_closing_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseClosingError("Closing session not found")
            if session["status"] == "cancelled":
                return self._dict(session)
            if session["status"] not in ("collecting", "ready"):
                raise WarehouseClosingError("Closing session cannot be cancelled")
            connection.execute(
                """
                UPDATE warehouse_closing_sessions
                SET status='cancelled', current_requirement_id=NULL,
                    last_activity_at_utc=?, revision=revision+1 WHERE id=?
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE warehouse_shifts SET status='open', updated_at_utc=?,
                    revision=revision+1 WHERE id=? AND status='waiting_required'
                """,
                (now, session["shift_id"]),
            )
            self._audit(
                connection,
                shift_id=int(session["shift_id"]),
                operation_id=None,
                actor_user_id=actor_user_id,
                event_type="closing_cancelled",
                entity_type="closing_session",
                entity_id=session_id,
                occurred_at_utc=now,
                before={"status": session["status"]},
                after={"status": "cancelled"},
                correlation_id=f"closing:{session_id}",
            )
        result = self.session(session_id)
        assert result is not None
        return result

    def finalize(
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
                "SELECT * FROM warehouse_closing_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise WarehouseClosingError("Closing session not found")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id=?",
                (session["shift_id"],),
            ).fetchone()
            if shift is None:
                raise WarehouseClosingError("Shift not found")
            if session["status"] == "completed" and shift["status"] == "closing":
                return {"session": self._dict(session), "shift": self._dict(shift)}
            if int(session["revision"]) != expected_revision:
                raise WarehouseClosingConflict("Closing session revision conflict")
            if session["status"] not in ("collecting", "ready"):
                raise WarehouseClosingError("Closing session is not ready")
            if shift["status"] not in ("open", "waiting_required"):
                raise WarehouseClosingError("Shift cannot enter closing")
            self._recalculate(connection, int(shift["id"]), now)
            if self._next_requirement(connection, int(shift["id"])) is not None:
                raise WarehouseClosingError("Required positions remain pending")
            session_cursor = connection.execute(
                """
                UPDATE warehouse_closing_sessions
                SET status='committing', last_activity_at_utc=?, revision=revision+1
                WHERE id=? AND revision=? AND status IN ('collecting','ready')
                """,
                (now, session_id, expected_revision),
            )
            if session_cursor.rowcount != 1:
                raise WarehouseClosingConflict("Closing session revision conflict")
            shift_cursor = connection.execute(
                """
                UPDATE warehouse_shifts SET status='closing', updated_at_utc=?,
                    revision=revision+1
                WHERE id=? AND revision=? AND status IN ('open','waiting_required')
                """,
                (now, shift["id"], shift["revision"]),
            )
            if shift_cursor.rowcount != 1:
                raise WarehouseClosingConflict("Shift revision conflict")
            connection.execute(
                """
                UPDATE warehouse_closing_sessions
                SET status='completed', current_requirement_id=NULL,
                    completed_at_utc=?, last_activity_at_utc=?, revision=revision+1
                WHERE id=? AND status='committing'
                """,
                (now, now, session_id),
            )
            self._audit(
                connection,
                shift_id=int(shift["id"]),
                operation_id=None,
                actor_user_id=actor_user_id,
                event_type="shift_prepared_for_report",
                entity_type="shift",
                entity_id=int(shift["id"]),
                occurred_at_utc=now,
                before={"status": shift["status"]},
                after={"status": "closing"},
                correlation_id=f"closing:{session_id}",
            )
        result = self.session(session_id)
        assert result is not None
        return result
