"""Transactional persistence for warehouse intake drafts and operations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.models.warehouse.nomenclature import BaseUnit
from app.services.warehouse.resolution_service import LocationOption, ProductOption


class WarehouseOperationsError(RuntimeError):
    pass


class WarehouseRevisionConflict(WarehouseOperationsError):
    pass


class WarehouseOperationsDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
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

    def runtime_settings(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.timezone, s.cutoff_local_time,
                       l.id AS source_location_id, l.name AS source_location_name
                FROM warehouse_settings s
                JOIN warehouse_locations l ON l.id = s.default_source_location_id
                WHERE s.id = 1 AND l.is_active = 1 AND l.can_be_source = 1
                """
            ).fetchone()
            if row is None:
                raise WarehouseOperationsError("Active default source is missing")
            return self._dict(row)

    def products(self) -> tuple[ProductOption, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, official_name, normalized_name, base_unit
                FROM warehouse_products WHERE is_active = 1 ORDER BY id
                """
            ).fetchall()
            result: list[ProductOption] = []
            for row in rows:
                product_id = int(row["id"])
                aliases = tuple(
                    str(item[0])
                    for item in connection.execute(
                        "SELECT alias FROM warehouse_product_aliases "
                        "WHERE product_id = ? ORDER BY normalized_alias",
                        (product_id,),
                    ).fetchall()
                )
                operations = frozenset(
                    str(item[0])
                    for item in connection.execute(
                        "SELECT operation_type FROM warehouse_product_operation_types "
                        "WHERE product_id = ?",
                        (product_id,),
                    ).fetchall()
                )
                result.append(
                    ProductOption(
                        id=product_id,
                        official_name=str(row["official_name"]),
                        normalized_name=str(row["normalized_name"]),
                        base_unit=BaseUnit(str(row["base_unit"])),
                        aliases=aliases,
                        operation_types=operations,
                    )
                )
            return tuple(result)

    def active_product_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM warehouse_products WHERE is_active=1"
                ).fetchone()[0]
            )

    def locations(self) -> tuple[LocationOption, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, normalized_name, can_be_source, can_be_destination
                FROM warehouse_locations WHERE is_active = 1 ORDER BY id
                """
            ).fetchall()
            return tuple(
                LocationOption(
                    id=int(row["id"]),
                    name=str(row["name"]),
                    normalized_name=str(row["normalized_name"]),
                    can_be_source=bool(row["can_be_source"]),
                    can_be_destination=bool(row["can_be_destination"]),
                )
                for row in rows
            )

    def product(self, product_id: int) -> ProductOption | None:
        return next((item for item in self.products() if item.id == product_id), None)

    def location(self, location_id: int) -> LocationOption | None:
        return next((item for item in self.locations() if item.id == location_id), None)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        shift_id: int | None,
        operation_id: int | None,
        actor_user_id: int | None,
        event_type: str,
        entity_type: str,
        entity_id: int,
        occurred_at_utc: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason_text: str | None = None,
        source_raw_text: str | None = None,
        correlation_id: str,
        actor_kind: str = "user",
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, encode(before), encode(after),
                reason_text, source_raw_text, correlation_id,
            ),
        )

    def batch_by_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM warehouse_intake_batches "
                "WHERE source_chat_id = ? AND source_message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            return self._load_batch(connection, int(row["id"])) if row else None

    def development_draft_by_message(
        self, chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts "
                "WHERE source_chat_id=? AND source_message_id=?",
                (chat_id, message_id),
            ).fetchone()
            return self._dict(row) if row else None

    def active_development_draft_for_actor(
        self,
        actor_user_id: int,
        source_location_id: int,
        business_date: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT d.* FROM warehouse_development_title_drafts d
                JOIN warehouse_shifts s ON s.id=d.shift_id
                WHERE d.actor_user_id=? AND s.source_location_id=?
                  AND s.business_date=?
                  AND d.status IN ('needs_title', 'needs_ingredients')
                ORDER BY d.id LIMIT 1
                """,
                (actor_user_id, source_location_id, business_date),
            ).fetchone()
            return self._dict(row) if row else None

    def create_development_title_draft(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts "
                "WHERE source_chat_id=? AND source_message_id=?",
                (payload["source_chat_id"], payload["source_message_id"]),
            ).fetchone()
            if existing is not None:
                return self._dict(existing)
            shift_id = self._ensure_open_shift(connection, payload)
            active = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts "
                "WHERE shift_id=? AND actor_user_id=? "
                "AND status IN ('needs_title', 'needs_ingredients') "
                "ORDER BY id LIMIT 1",
                (shift_id, payload["actor_user_id"]),
            ).fetchone()
            if active is not None:
                return self._dict(active)
            cursor = connection.execute(
                """
                INSERT INTO warehouse_development_title_drafts (
                    shift_id, actor_user_id, source_chat_id, source_message_id,
                    raw_text, development_title, ingredients_text, status, batch_id,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, 'needs_title', NULL, ?, ?)
                """,
                (
                    shift_id, payload["actor_user_id"], payload["source_chat_id"],
                    payload["source_message_id"], payload["raw_text"],
                    payload["ingredients_text"], payload["now"], payload["now"],
                ),
            )
            draft_id = int(cursor.lastrowid)
            self._audit(
                connection, shift_id=shift_id, operation_id=None,
                actor_user_id=payload["actor_user_id"],
                event_type="development_title_requested",
                entity_type="development_draft", entity_id=draft_id,
                occurred_at_utc=payload["now"], after={"status": "needs_title"},
                source_raw_text=payload["raw_text"],
                correlation_id=f"development-draft:{draft_id}",
            )
            row = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            assert row is not None
            return self._dict(row)

    def create_development_ingredients_draft(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts "
                "WHERE source_chat_id=? AND source_message_id=?",
                (payload["source_chat_id"], payload["source_message_id"]),
            ).fetchone()
            if existing is not None:
                return self._dict(existing)
            shift_id = self._ensure_open_shift(connection, payload)
            active = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts "
                "WHERE shift_id=? AND actor_user_id=? "
                "AND status IN ('needs_title', 'needs_ingredients') "
                "ORDER BY id LIMIT 1",
                (shift_id, payload["actor_user_id"]),
            ).fetchone()
            if active is not None:
                return self._dict(active)
            cursor = connection.execute(
                """
                INSERT INTO warehouse_development_title_drafts (
                    shift_id, actor_user_id, source_chat_id, source_message_id,
                    raw_text, development_title, ingredients_text, status,
                    batch_id, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'needs_ingredients', NULL, ?, ?)
                """,
                (
                    shift_id, payload["actor_user_id"], payload["source_chat_id"],
                    payload["source_message_id"], payload["raw_text"],
                    payload["development_title"], payload["now"], payload["now"],
                ),
            )
            draft_id = int(cursor.lastrowid)
            self._audit(
                connection, shift_id=shift_id, operation_id=None,
                actor_user_id=payload["actor_user_id"],
                event_type="development_ingredients_requested",
                entity_type="development_draft", entity_id=draft_id,
                occurred_at_utc=payload["now"],
                after={
                    "status": "needs_ingredients",
                    "development_title": payload["development_title"],
                },
                source_raw_text=payload["raw_text"],
                correlation_id=f"development-draft:{draft_id}",
            )
            row = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            assert row is not None
            return self._dict(row)

    def promote_development_title_draft(
        self,
        draft_id: int,
        batch_payload: dict[str, Any],
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        """Atomically turn a missing-title draft into a normal intake batch."""

        with self._transaction() as connection:
            draft = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None or int(draft["actor_user_id"]) != actor_user_id:
                raise WarehouseOperationsError("Development draft not found")
            if draft["status"] == "promoted" and draft["batch_id"] is not None:
                result = self._load_batch(connection, int(draft["batch_id"]))
                assert result is not None
                return result
            if draft["status"] not in ("needs_title", "needs_ingredients"):
                raise WarehouseOperationsError("Development draft is not active")
            shift = connection.execute(
                "SELECT status FROM warehouse_shifts WHERE id=?",
                (draft["shift_id"],),
            ).fetchone()
            if shift is None or shift["status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            batch_id = self._insert_batch(
                connection,
                batch_payload,
                shift_id=int(draft["shift_id"]),
            )
            connection.execute(
                "UPDATE warehouse_development_title_drafts SET status='promoted', "
                "development_title=?, ingredients_text=?, batch_id=?, "
                "updated_at_utc=? WHERE id=? "
                "AND status IN ('needs_title', 'needs_ingredients')",
                (
                    batch_payload["development_title"],
                    batch_payload["ingredients_text"],
                    batch_id,
                    batch_payload["now"],
                    draft_id,
                ),
            )
            self._audit(
                connection, shift_id=int(draft["shift_id"]), operation_id=None,
                actor_user_id=actor_user_id,
                event_type=(
                    "development_ingredients_supplied"
                    if draft["status"] == "needs_ingredients"
                    else "development_title_supplied"
                ),
                entity_type="development_draft", entity_id=draft_id,
                occurred_at_utc=batch_payload["now"],
                before={"status": str(draft["status"])},
                after={"status": "promoted", "batch_id": batch_id},
                source_raw_text=str(draft["raw_text"]),
                correlation_id=f"batch:{batch_id}",
            )
            result = self._load_batch(connection, batch_id)
            assert result is not None
            return result

    def cancel_development_title_draft(
        self, draft_id: int, *, actor_user_id: int, now: str
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            draft = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None or int(draft["actor_user_id"]) != actor_user_id:
                raise WarehouseOperationsError("Development draft not found")
            if draft["status"] not in ("needs_title", "needs_ingredients"):
                raise WarehouseOperationsError("Development draft is not active")
            connection.execute(
                "UPDATE warehouse_development_title_drafts SET status='cancelled', "
                "updated_at_utc=? WHERE id=?",
                (now, draft_id),
            )
            self._audit(
                connection, shift_id=int(draft["shift_id"]), operation_id=None,
                actor_user_id=actor_user_id,
                event_type="development_draft_cancelled",
                entity_type="development_draft", entity_id=draft_id,
                occurred_at_utc=now, before={"status": str(draft["status"])},
                after={"status": "cancelled"},
                source_raw_text=str(draft["raw_text"]),
                correlation_id=f"development-draft:{draft_id}",
            )
            updated = connection.execute(
                "SELECT * FROM warehouse_development_title_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            assert updated is not None
            return self._dict(updated)

    def active_batch_for_actor(
        self,
        actor_user_id: int,
        source_location_id: int,
        business_date: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT b.id FROM warehouse_intake_batches b
                JOIN warehouse_shifts s ON s.id=b.shift_id
                WHERE b.actor_user_id=? AND s.source_location_id=?
                  AND s.business_date=?
                  AND b.status IN ('ready', 'needs_resolution')
                ORDER BY b.id LIMIT 1
                """,
                (actor_user_id, source_location_id, business_date),
            ).fetchone()
            return self._load_batch(connection, int(row["id"])) if row else None

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._load_batch(connection, batch_id)

    def _load_batch(
        self, connection: sqlite3.Connection, batch_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM warehouse_intake_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        batch = self._dict(row)
        lines: list[dict[str, Any]] = []
        for line_row in connection.execute(
            """
            SELECT l.*, p.official_name AS product_name,
                   d.name AS destination_name
            FROM warehouse_intake_lines l
            LEFT JOIN warehouse_products p ON p.id=l.product_id
            LEFT JOIN warehouse_locations d ON d.id=l.destination_location_id
            WHERE l.batch_id=? ORDER BY l.line_number
            """,
            (batch_id,),
        ).fetchall():
            line = self._dict(line_row)
            line["candidates"] = [
                self._dict(candidate)
                for candidate in connection.execute(
                    """
                    SELECT c.product_id, p.official_name, c.score_scaled, c.rank
                    FROM warehouse_intake_candidates c
                    JOIN warehouse_products p ON p.id = c.product_id
                    WHERE c.line_id = ? ORDER BY c.rank
                    """,
                    (int(line_row["id"]),),
                ).fetchall()
            ]
            lines.append(line)
        batch["lines"] = lines
        return batch

    @staticmethod
    def _ensure_open_shift(
        connection: sqlite3.Connection, payload: dict[str, Any]
    ) -> int:
        shift = connection.execute(
            "SELECT id, status FROM warehouse_shifts WHERE source_location_id=? "
            "AND business_date=?",
            (payload["source_location_id"], payload["business_date"]),
        ).fetchone()
        if shift is not None:
            if shift["status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            return int(shift["id"])
        cursor = connection.execute(
            """
            INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot,
                cutoff_snapshot, status, started_at_utc,
                scheduled_close_at_utc, closed_at_utc, closed_by, revision,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, NULL, 1, ?, ?)
            """,
            (
                payload["source_location_id"], payload["business_date"],
                payload["timezone"], payload["cutoff"], payload["started_at_utc"],
                payload["scheduled_close_at_utc"], payload["now"], payload["now"],
            ),
        )
        return int(cursor.lastrowid)

    def _insert_batch(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        *,
        shift_id: int,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO warehouse_intake_batches (
                shift_id, actor_user_id, source_chat_id, source_message_id,
                operation_type, development_title, raw_text, status,
                created_at_utc, updated_at_utc, committed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                shift_id, payload["actor_user_id"], payload["source_chat_id"],
                payload["source_message_id"], payload["operation_type"],
                payload.get("development_title"), payload["raw_text"],
                payload["status"], payload["now"], payload["now"],
            ),
        )
        batch_id = int(cursor.lastrowid)
        for line in payload["lines"]:
            cursor = connection.execute(
                """
                INSERT INTO warehouse_intake_lines (
                    batch_id, line_number, raw_line, status, product_text,
                    product_id, input_quantity_text, input_unit_text,
                    quantity_scaled, base_unit, reason_text, comment_text,
                    destination_text, destination_location_id, error_code,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id, line["line_number"], line["raw_line"], line["status"],
                    line["product_text"], line.get("product_id"),
                    line["input_quantity_text"], line.get("input_unit_text"),
                    line.get("quantity_scaled"), line.get("base_unit"),
                    line.get("reason_text"), line.get("comment_text"),
                    line.get("destination_text"), line.get("destination_location_id"),
                    line.get("error_code"), payload["now"], payload["now"],
                ),
            )
            line_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO warehouse_intake_candidates "
                "(line_id, product_id, score_scaled, rank) VALUES (?, ?, ?, ?)",
                [
                    (line_id, item[0], item[2], rank)
                    for rank, item in enumerate(line.get("candidates", ()), start=1)
                ],
            )
        event = (
            "development_batch_created"
            if payload["operation_type"] == "development"
            else "batch_prepared"
        )
        self._audit(
            connection, shift_id=shift_id, operation_id=None,
            actor_user_id=payload["actor_user_id"], event_type=event,
            entity_type="intake_batch", entity_id=batch_id,
            occurred_at_utc=payload["now"],
            after={
                "status": payload["status"],
                "development_title": payload.get("development_title"),
            },
            source_raw_text=payload["raw_text"], correlation_id=f"batch:{batch_id}",
        )
        return batch_id

    def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM warehouse_intake_batches "
                "WHERE source_chat_id = ? AND source_message_id = ?",
                (payload["source_chat_id"], payload["source_message_id"]),
            ).fetchone()
            if existing:
                result = self._load_batch(connection, int(existing["id"]))
                assert result is not None
                return result
            shift_id = self._ensure_open_shift(connection, payload)
            active = connection.execute(
                "SELECT id FROM warehouse_intake_batches "
                "WHERE shift_id=? AND actor_user_id=? "
                "AND status IN ('ready', 'needs_resolution') "
                "ORDER BY id LIMIT 1",
                (shift_id, payload["actor_user_id"]),
            ).fetchone()
            if active is not None:
                result = self._load_batch(connection, int(active["id"]))
                assert result is not None
                return result
            batch_id = self._insert_batch(connection, payload, shift_id=shift_id)
            result = self._load_batch(connection, batch_id)
            assert result is not None
            return result

    def remove_line(
        self,
        batch_id: int,
        line_id: int,
        *,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM warehouse_intake_batches WHERE id=?", (batch_id,)
            ).fetchone()
            line = connection.execute(
                "SELECT * FROM warehouse_intake_lines WHERE id=? AND batch_id=?",
                (line_id, batch_id),
            ).fetchone()
            if batch is None or line is None:
                raise WarehouseOperationsError("Batch line not found")
            if batch["status"] not in ("ready", "needs_resolution"):
                raise WarehouseOperationsError("Batch cannot be changed")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM warehouse_intake_lines WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()[0]
            )
            if count == 1:
                connection.execute(
                    "UPDATE warehouse_intake_batches SET status='cancelled', "
                    "updated_at_utc=? WHERE id=?", (now, batch_id)
                )
                connection.execute(
                    "UPDATE warehouse_intake_lines SET status='cancelled', "
                    "updated_at_utc=? WHERE id=?", (now, line_id)
                )
                status = "cancelled"
            else:
                connection.execute(
                    "DELETE FROM warehouse_intake_lines WHERE id=?", (line_id,)
                )
                status = self._refresh_batch_status(connection, batch_id, now)
            self._audit(
                connection, shift_id=int(batch["shift_id"]), operation_id=None,
                actor_user_id=actor_user_id, event_type="batch_line_removed",
                entity_type="intake_line", entity_id=line_id, occurred_at_utc=now,
                before=self._dict(line), after={"batch_status": status},
                source_raw_text=str(line["raw_line"]),
                correlation_id=f"batch:{batch_id}",
            )
            result = self._load_batch(connection, batch_id)
            assert result is not None
            return result

    @staticmethod
    def _refresh_batch_status(
        connection: sqlite3.Connection, batch_id: int, now: str
    ) -> str:
        unresolved = int(
            connection.execute(
                "SELECT COUNT(*) FROM warehouse_intake_lines "
                "WHERE batch_id = ? AND status <> 'resolved'",
                (batch_id,),
            ).fetchone()[0]
        )
        status = "ready" if unresolved == 0 else "needs_resolution"
        connection.execute(
            "UPDATE warehouse_intake_batches SET status = ?, updated_at_utc = ? "
            "WHERE id = ? AND status IN ('ready', 'needs_resolution')",
            (status, now, batch_id),
        )
        return status

    def replace_line(
        self,
        batch_id: int,
        line_id: int,
        line: dict[str, Any],
        *,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM warehouse_intake_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            current = connection.execute(
                "SELECT * FROM warehouse_intake_lines WHERE id = ? AND batch_id = ?",
                (line_id, batch_id),
            ).fetchone()
            if batch is None or current is None:
                raise WarehouseOperationsError("Batch line not found")
            if batch["status"] not in ("ready", "needs_resolution"):
                raise WarehouseOperationsError("Batch cannot be changed")
            connection.execute(
                """
                UPDATE warehouse_intake_lines SET
                    raw_line=?, status=?, product_text=?, product_id=?,
                    input_quantity_text=?, input_unit_text=?, quantity_scaled=?,
                    base_unit=?, reason_text=?, comment_text=?, destination_text=?,
                    destination_location_id=?, error_code=?, updated_at_utc=?
                WHERE id=?
                """,
                (
                    line["raw_line"], line["status"], line["product_text"],
                    line.get("product_id"), line["input_quantity_text"],
                    line.get("input_unit_text"), line.get("quantity_scaled"),
                    line.get("base_unit"), line.get("reason_text"),
                    line.get("comment_text"), line.get("destination_text"),
                    line.get("destination_location_id"), line.get("error_code"),
                    now, line_id,
                ),
            )
            connection.execute(
                "DELETE FROM warehouse_intake_candidates WHERE line_id = ?", (line_id,)
            )
            connection.executemany(
                "INSERT INTO warehouse_intake_candidates "
                "(line_id, product_id, score_scaled, rank) VALUES (?, ?, ?, ?)",
                [
                    (line_id, item[0], item[2], rank)
                    for rank, item in enumerate(line.get("candidates", ()), start=1)
                ],
            )
            status = self._refresh_batch_status(connection, batch_id, now)
            if batch["operation_type"] == "development":
                event_type = (
                    "development_product_resolved"
                    if current["status"] == "ambiguous"
                    else "development_batch_line_changed"
                )
            else:
                event_type = "batch_line_changed"
            self._audit(
                connection, shift_id=int(batch["shift_id"]), operation_id=None,
                actor_user_id=actor_user_id, event_type=event_type,
                entity_type="intake_line", entity_id=line_id, occurred_at_utc=now,
                before=self._dict(current), after={"status": line["status"]},
                source_raw_text=line["raw_line"], correlation_id=f"batch:{batch_id}",
            )
            result = self._load_batch(connection, batch_id)
            assert result is not None and result["status"] == status
            return result

    def cancel_batch(self, batch_id: int, *, actor_user_id: int, now: str) -> dict[str, Any]:
        with self._transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM warehouse_intake_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise WarehouseOperationsError("Batch not found")
            if batch["status"] not in ("ready", "needs_resolution"):
                raise WarehouseOperationsError("Batch cannot be cancelled")
            connection.execute(
                "UPDATE warehouse_intake_batches SET status='cancelled', "
                "updated_at_utc=? WHERE id=?", (now, batch_id)
            )
            connection.execute(
                "UPDATE warehouse_intake_lines SET status='cancelled', "
                "updated_at_utc=? WHERE batch_id=?", (now, batch_id)
            )
            self._audit(
                connection, shift_id=int(batch["shift_id"]), operation_id=None,
                actor_user_id=actor_user_id,
                event_type=(
                    "development_draft_cancelled"
                    if batch["operation_type"] == "development"
                    else "batch_cancelled"
                ),
                entity_type="intake_batch", entity_id=batch_id, occurred_at_utc=now,
                before={"status": batch["status"]}, after={"status": "cancelled"},
                source_raw_text=str(batch["raw_text"]), correlation_id=f"batch:{batch_id}",
            )
            result = self._load_batch(connection, batch_id)
            assert result is not None
            return result

    def commit_batch(
        self,
        batch_id: int,
        *,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            batch = connection.execute(
                "SELECT * FROM warehouse_intake_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None or batch["status"] != "ready":
                raise WarehouseOperationsError("Batch is not ready")
            shift = connection.execute(
                "SELECT * FROM warehouse_shifts WHERE id = ?", (batch["shift_id"],)
            ).fetchone()
            if shift is None or shift["status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            lines = connection.execute(
                "SELECT * FROM warehouse_intake_lines WHERE batch_id=? ORDER BY line_number",
                (batch_id,),
            ).fetchall()
            if not lines or any(line["status"] != "resolved" for line in lines):
                raise WarehouseOperationsError("Batch contains unresolved lines")
            operations: list[dict[str, Any]] = []
            required_type = (
                "product_writeoff"
                if batch["operation_type"] == "writeoff"
                else str(batch["operation_type"])
            )
            development_title = batch["development_title"]
            if batch["operation_type"] == "development" and (
                development_title is None
                or not str(development_title).strip()
                or len(str(development_title)) > 200
            ):
                raise WarehouseOperationsError("Development title is invalid")
            for line in lines:
                product = connection.execute(
                    """
                    SELECT p.* FROM warehouse_products p
                    JOIN warehouse_product_operation_types t ON t.product_id=p.id
                    WHERE p.id=? AND p.is_active=1 AND t.operation_type=?
                    """,
                    (line["product_id"], required_type),
                ).fetchone()
                if product is None or product["base_unit"] != line["base_unit"]:
                    raise WarehouseOperationsError("Product changed before commit")
                destination_id = line["destination_location_id"]
                if batch["operation_type"] == "transfer":
                    destination = connection.execute(
                        "SELECT 1 FROM warehouse_locations WHERE id=? AND is_active=1 "
                        "AND can_be_destination=1",
                        (destination_id,),
                    ).fetchone()
                    if destination is None or destination_id == shift["source_location_id"]:
                        raise WarehouseOperationsError("Destination changed before commit")
                cursor = connection.execute(
                    """
                    INSERT INTO warehouse_operations (
                        shift_id, operation_type, development_title,
                        development_group_id, product_id, product_name_snapshot,
                        quantity_scaled, base_unit, input_quantity_text, input_unit_text,
                        source_location_id, destination_location_id, reason_text,
                        comment_text, raw_text, author_user_id, occurred_at_utc,
                        intake_line_id, origin, is_deleted, deleted_at_utc, deleted_by,
                        deletion_reason, created_at_utc, updated_at_utc, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'message',
                              0, NULL, NULL, NULL, ?, ?, 1)
                    """,
                    (
                        shift["id"], batch["operation_type"], development_title,
                        batch_id if batch["operation_type"] == "development" else None,
                        product["id"],
                        product["official_name"], line["quantity_scaled"],
                        line["base_unit"], line["input_quantity_text"],
                        line["input_unit_text"], shift["source_location_id"],
                        destination_id, line["reason_text"], line["comment_text"],
                        line["raw_line"], actor_user_id, now, line["id"], now, now,
                    ),
                )
                operation_id = int(cursor.lastrowid)
                operation = self._dict(
                    connection.execute(
                        "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
                    ).fetchone()
                )
                operations.append(operation)
                self._audit(
                    connection, shift_id=int(shift["id"]), operation_id=operation_id,
                    actor_user_id=actor_user_id,
                    event_type=(
                        "development_operation_created"
                        if batch["operation_type"] == "development"
                        else "operation_created"
                    ),
                    entity_type="operation", entity_id=operation_id,
                    occurred_at_utc=now, after=operation,
                    source_raw_text=str(line["raw_line"]),
                    correlation_id=f"batch:{batch_id}",
                )
            connection.execute(
                "UPDATE warehouse_intake_lines SET status='committed', "
                "updated_at_utc=? WHERE batch_id=?", (now, batch_id)
            )
            connection.execute(
                "UPDATE warehouse_intake_batches SET status='committed', "
                "committed_at_utc=?, updated_at_utc=? WHERE id=?", (now, now, batch_id)
            )
            self._audit(
                connection, shift_id=int(shift["id"]), operation_id=None,
                actor_user_id=actor_user_id,
                event_type=(
                    "development_batch_committed"
                    if batch["operation_type"] == "development"
                    else "batch_committed"
                ),
                entity_type="intake_batch", entity_id=batch_id, occurred_at_utc=now,
                before={"status": "ready"}, after={"status": "committed"},
                source_raw_text=str(batch["raw_text"]), correlation_id=f"batch:{batch_id}",
            )
            return operations

    def create_manual_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as connection:
            required_type = (
                "product_writeoff"
                if payload["operation_type"] == "writeoff" else "transfer"
            )
            product = connection.execute(
                """
                SELECT p.id FROM warehouse_products p
                JOIN warehouse_product_operation_types t ON t.product_id=p.id
                WHERE p.id=? AND p.is_active=1 AND p.base_unit=?
                  AND t.operation_type=?
                """,
                (payload["product_id"], payload["base_unit"], required_type),
            ).fetchone()
            if product is None:
                raise WarehouseOperationsError("Product changed before manual create")
            if payload["operation_type"] == "transfer":
                destination = connection.execute(
                    "SELECT id FROM warehouse_locations WHERE id=? AND is_active=1 "
                    "AND can_be_destination=1",
                    (payload.get("destination_location_id"),),
                ).fetchone()
                if (
                    destination is None
                    or int(destination["id"]) == int(payload["source_location_id"])
                ):
                    raise WarehouseOperationsError(
                        "Destination changed before manual create"
                    )
            shift = connection.execute(
                "SELECT id, status FROM warehouse_shifts WHERE source_location_id=? "
                "AND business_date=?",
                (payload["source_location_id"], payload["business_date"]),
            ).fetchone()
            if shift is None:
                cursor = connection.execute(
                    """
                    INSERT INTO warehouse_shifts (
                        source_location_id, business_date, timezone_snapshot,
                        cutoff_snapshot, status, started_at_utc,
                        scheduled_close_at_utc, revision, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, 1, ?, ?)
                    """,
                    (
                        payload["source_location_id"], payload["business_date"],
                        payload["timezone"], payload["cutoff"],
                        payload["started_at_utc"], payload["scheduled_close_at_utc"],
                        payload["now"], payload["now"],
                    ),
                )
                shift_id = int(cursor.lastrowid)
            else:
                if shift["status"] not in ("open", "waiting_required"):
                    raise WarehouseOperationsError("Shift is not open")
                shift_id = int(shift["id"])
            cursor = connection.execute(
                """
                INSERT INTO warehouse_operations (
                    shift_id, operation_type, product_id, product_name_snapshot,
                    quantity_scaled, base_unit, input_quantity_text, input_unit_text,
                    source_location_id, destination_location_id, reason_text,
                    comment_text, raw_text, author_user_id, occurred_at_utc,
                    intake_line_id, origin, is_deleted, created_at_utc,
                    updated_at_utc, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                          'manual', 0, ?, ?, 1)
                """,
                (
                    shift_id, payload["operation_type"], payload["product_id"],
                    payload["product_name_snapshot"], payload["quantity_scaled"],
                    payload["base_unit"], payload["input_quantity_text"],
                    payload.get("input_unit_text"), payload["source_location_id"],
                    payload.get("destination_location_id"), payload.get("reason_text"),
                    payload.get("comment_text"), payload["raw_text"],
                    payload["actor_user_id"], payload["now"], payload["now"],
                    payload["now"],
                ),
            )
            operation_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert row is not None
            operation = self._dict(row)
            self._audit(
                connection, shift_id=shift_id, operation_id=operation_id,
                actor_user_id=payload["actor_user_id"], event_type="operation_created",
                entity_type="operation", entity_id=operation_id,
                occurred_at_utc=payload["now"], after=operation,
                source_raw_text=payload["raw_text"],
                correlation_id=f"operation:{operation_id}:manual",
            )
            return operation

    def list_operations(self, shift_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                self._dict(row)
                for row in connection.execute(
                    "SELECT * FROM warehouse_operations WHERE shift_id=? "
                    "AND is_deleted=0 "
                    "ORDER BY occurred_at_utc, id", (shift_id,)
                ).fetchall()
            ]

    def get_operation(self, operation_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            return self._dict(row) if row else None

    def shift(self, shift_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, l.name AS source_location_name
                FROM warehouse_shifts s
                JOIN warehouse_locations l ON l.id=s.source_location_id
                WHERE s.id=?
                """,
                (shift_id,),
            ).fetchone()
            return self._dict(row) if row else None

    def shift_for_date(
        self, source_location_id: int, business_date: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, l.name AS source_location_name
                FROM warehouse_shifts s
                JOIN warehouse_locations l ON l.id=s.source_location_id
                WHERE s.source_location_id=? AND s.business_date=?
                """,
                (source_location_id, business_date),
            ).fetchone()
            return self._dict(row) if row else None

    def operation_view(self, operation_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT o.*, s.status AS shift_status,
                       src.name AS source_location_name,
                       dst.name AS destination_location_name
                FROM warehouse_operations o
                JOIN warehouse_shifts s ON s.id=o.shift_id
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst ON dst.id=o.destination_location_id
                WHERE o.id=?
                """,
                (operation_id,),
            ).fetchone()
            return self._dict(row) if row else None

    def operation_page(
        self,
        shift_id: int,
        operation_type: str,
        *,
        page: int,
        page_size: int = 8,
    ) -> tuple[list[dict[str, Any]], int]:
        offset = max(page, 0) * page_size
        with self._connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM warehouse_operations "
                    "WHERE shift_id=? AND operation_type=? AND is_deleted=0",
                    (shift_id, operation_type),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT o.*, src.name AS source_location_name,
                       dst.name AS destination_location_name
                FROM warehouse_operations o
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst ON dst.id=o.destination_location_id
                WHERE o.shift_id=? AND o.operation_type=? AND o.is_deleted=0
                ORDER BY o.occurred_at_utc, o.id LIMIT ? OFFSET ?
                """,
                (shift_id, operation_type, page_size, offset),
            ).fetchall()
            return [self._dict(row) for row in rows], total

    def aggregates(self, shift_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT o.operation_type, o.product_id, o.product_name_snapshot,
                       o.base_unit, o.source_location_id,
                       o.destination_location_id,
                       o.development_title, o.development_group_id,
                       src.name AS source_location_name,
                       dst.name AS destination_location_name,
                       SUM(o.quantity_scaled) AS quantity_scaled,
                       COUNT(*) AS operation_count
                FROM warehouse_operations o
                JOIN warehouse_locations src ON src.id=o.source_location_id
                LEFT JOIN warehouse_locations dst
                  ON dst.id=o.destination_location_id
                WHERE o.shift_id=? AND o.is_deleted=0 AND o.quantity_scaled>0
                GROUP BY o.operation_type, o.product_id, o.product_name_snapshot,
                         o.base_unit, o.source_location_id,
                         o.destination_location_id, o.development_title,
                         o.development_group_id
                ORDER BY o.operation_type, o.product_name_snapshot,
                         o.destination_location_id, o.development_group_id
                """,
                (shift_id,),
            ).fetchall()
            return [self._dict(row) for row in rows]

    def update_operation(
        self,
        operation_id: int,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        reason_text: str | None = None,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "product_id", "product_name_snapshot", "quantity_scaled", "base_unit",
            "input_quantity_text", "input_unit_text", "reason_text", "comment_text",
            "destination_location_id",
        }
        if not changes or not set(changes) <= allowed:
            raise WarehouseOperationsError("Invalid operation changes")
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            current = connection.execute(
                """
                SELECT o.*, s.status AS shift_status FROM warehouse_operations o
                JOIN warehouse_shifts s ON s.id=o.shift_id WHERE o.id=?
                """,
                (operation_id,),
            ).fetchone()
            if current is None:
                raise WarehouseOperationsError("Operation not found")
            if int(current["revision"]) != expected_revision:
                raise WarehouseRevisionConflict("Operation revision conflict")
            if current["is_deleted"]:
                raise WarehouseOperationsError("Deleted operation cannot be changed")
            if current["shift_status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            proposed = self._dict(current)
            proposed.update(changes)
            required_type = (
                "product_writeoff"
                if proposed["operation_type"] == "writeoff"
                else str(proposed["operation_type"])
            )
            valid_product = connection.execute(
                """
                SELECT 1 FROM warehouse_products p
                JOIN warehouse_product_operation_types t ON t.product_id=p.id
                WHERE p.id=? AND p.is_active=1 AND p.base_unit=?
                  AND t.operation_type=?
                """,
                (proposed["product_id"], proposed["base_unit"], required_type),
            ).fetchone()
            if valid_product is None:
                raise WarehouseOperationsError("Invalid operation product")
            destination_id = proposed["destination_location_id"]
            if proposed["operation_type"] in {"writeoff", "development"}:
                if destination_id is not None:
                    raise WarehouseOperationsError(
                        "Writeoff/development cannot have destination"
                    )
            else:
                destination = connection.execute(
                    "SELECT 1 FROM warehouse_locations WHERE id=? AND is_active=1 "
                    "AND can_be_destination=1",
                    (destination_id,),
                ).fetchone()
                if destination is None or destination_id == proposed["source_location_id"]:
                    raise WarehouseOperationsError("Invalid operation destination")
            assignments = ", ".join(f"{key}=?" for key in changes)
            connection.execute(
                f"UPDATE warehouse_operations SET {assignments}, updated_at_utc=?, "
                "revision=revision+1 WHERE id=?",
                (*changes.values(), now, operation_id),
            )
            updated_row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert updated_row is not None
            updated = self._dict(updated_row)
            before = self._dict(current)
            before.pop("shift_status", None)
            self._audit(
                connection, shift_id=int(current["shift_id"]),
                operation_id=operation_id, actor_user_id=actor_user_id,
                event_type=(
                    "development_operation_updated"
                    if current["operation_type"] == "development"
                    else "operation_updated"
                ), entity_type="operation",
                entity_id=operation_id, occurred_at_utc=now, before=before,
                after=updated, reason_text=reason_text,
                source_raw_text=str(current["raw_text"]),
                correlation_id=f"operation:{operation_id}:r{updated['revision']}",
            )
            return updated

    def soft_delete_operation(
        self,
        operation_id: int,
        *,
        expected_revision: int,
        actor_user_id: int,
        deletion_reason: str,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        reason = deletion_reason.strip()
        if not reason:
            raise WarehouseOperationsError("Deletion reason is required")
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            current = connection.execute(
                """
                SELECT o.*, s.status AS shift_status FROM warehouse_operations o
                JOIN warehouse_shifts s ON s.id=o.shift_id WHERE o.id=?
                """,
                (operation_id,),
            ).fetchone()
            if current is None:
                raise WarehouseOperationsError("Operation not found")
            if int(current["revision"]) != expected_revision:
                raise WarehouseRevisionConflict("Operation revision conflict")
            if current["is_deleted"]:
                raise WarehouseOperationsError("Operation is already deleted")
            if current["shift_status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            connection.execute(
                """
                UPDATE warehouse_operations
                SET is_deleted=1, deleted_at_utc=?, deleted_by=?,
                    deletion_reason=?, updated_at_utc=?, revision=revision+1
                WHERE id=?
                """,
                (now, actor_user_id, reason, now, operation_id),
            )
            updated_row = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?", (operation_id,)
            ).fetchone()
            assert updated_row is not None
            updated = self._dict(updated_row)
            before = self._dict(current)
            before.pop("shift_status", None)
            self._audit(
                connection, shift_id=int(current["shift_id"]),
                operation_id=operation_id, actor_user_id=actor_user_id,
                event_type=(
                    "development_operation_deleted"
                    if current["operation_type"] == "development"
                    else "operation_deleted"
                ), entity_type="operation",
                entity_id=operation_id, occurred_at_utc=now, before=before,
                after=updated, reason_text=reason,
                source_raw_text=str(current["raw_text"]),
                correlation_id=f"operation:{operation_id}:r{updated['revision']}",
            )
            return updated

    def rename_development_group(
        self,
        operation_id: int,
        title: str,
        *,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        cleaned = title.strip()
        if not cleaned or len(cleaned) > 200:
            raise WarehouseOperationsError("Development title is invalid")
        with self._transaction() as connection:
            if authorize is not None:
                authorize()
            current = connection.execute(
                """
                SELECT o.*, s.status AS shift_status
                FROM warehouse_operations o
                JOIN warehouse_shifts s ON s.id=o.shift_id
                WHERE o.id=?
                """,
                (operation_id,),
            ).fetchone()
            if current is None or current["operation_type"] != "development":
                raise WarehouseOperationsError("Development operation not found")
            if int(current["revision"]) != expected_revision:
                raise WarehouseRevisionConflict("Operation revision conflict")
            if current["is_deleted"]:
                raise WarehouseOperationsError("Deleted operation cannot be changed")
            if current["shift_status"] not in ("open", "waiting_required"):
                raise WarehouseOperationsError("Shift is not open")
            group_id = int(current["development_group_id"])
            before_title = str(current["development_title"])
            connection.execute(
                "UPDATE warehouse_operations SET development_title=?, "
                "updated_at_utc=?, revision=revision+1 "
                "WHERE development_group_id=? AND operation_type='development'",
                (cleaned, now, group_id),
            )
            connection.execute(
                "UPDATE warehouse_intake_batches SET development_title=?, "
                "updated_at_utc=? WHERE id=? AND operation_type='development'",
                (cleaned, now, group_id),
            )
            updated = connection.execute(
                "SELECT * FROM warehouse_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
            assert updated is not None
            self._audit(
                connection, shift_id=int(current["shift_id"]),
                operation_id=operation_id, actor_user_id=actor_user_id,
                event_type="development_group_renamed",
                entity_type="development_group", entity_id=group_id,
                occurred_at_utc=now,
                before={"development_title": before_title},
                after={"development_title": cleaned},
                source_raw_text=str(current["raw_text"]),
                correlation_id=f"development-group:{group_id}",
            )
            return self._dict(updated)
