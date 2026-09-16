"""SQLite repository for report recipients and idempotent deliveries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class WarehouseDeliveryError(RuntimeError):
    """Raised when recipient or delivery state is invalid."""


class WarehouseRecipientConflict(WarehouseDeliveryError):
    """Raised on duplicate recipients or stale optimistic revisions."""


class WarehouseDeliveryDatabase:
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
        actor_user_id: int,
        event_type: str,
        entity_type: str,
        entity_id: int,
        occurred_at_utc: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        correlation_id: str,
        shift_id: int | None = None,
    ) -> None:
        def encode(value: dict[str, Any] | None) -> str | None:
            return (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if value is not None else None
            )

        connection.execute(
            """
            INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, before_json, after_json,
                reason_text, source_raw_text, correlation_id
            ) VALUES (?, NULL, 'user', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                shift_id, actor_user_id, event_type, entity_type, entity_id,
                occurred_at_utc, encode(before), encode(after), correlation_id,
            ),
        )

    @classmethod
    def _recipient(
        cls, connection: sqlite3.Connection, recipient_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM warehouse_report_recipients WHERE id=?",
            (recipient_id,),
        ).fetchone()
        return cls._dict(row) if row is not None else None

    def recipient(self, recipient_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._recipient(connection, recipient_id)

    def list_recipients(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM warehouse_report_recipients "
                "ORDER BY is_active DESC, title COLLATE NOCASE, id"
            ).fetchall()
            return [self._dict(row) for row in rows]

    def active_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM warehouse_report_recipients WHERE is_active=1"
            ).fetchone()[0])

    def create_recipient(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            authorize()
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO warehouse_report_recipients (
                        chat_id, title, chat_type, is_active, created_by,
                        created_at_utc, updated_at_utc, revision
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, 1)
                    """,
                    (chat_id, title, chat_type, actor_user_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                if connection.execute(
                    "SELECT 1 FROM warehouse_report_recipients WHERE chat_id=?",
                    (chat_id,),
                ).fetchone() is not None:
                    raise WarehouseRecipientConflict("Recipient already exists") from exc
                raise
            recipient_id = int(cursor.lastrowid)
            recipient = self._recipient(connection, recipient_id)
            assert recipient is not None
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                event_type="warehouse_report_recipient_created",
                entity_type="warehouse_report_recipient",
                entity_id=recipient_id,
                occurred_at_utc=now,
                before=None,
                after={
                    "chat_id": chat_id, "title": title,
                    "chat_type": chat_type, "is_active": 1, "revision": 1,
                },
                correlation_id=f"report-recipient:{recipient_id}",
            )
            return recipient

    def update_title(
        self,
        recipient_id: int,
        *,
        title: str,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            authorize()
            before = self._recipient(connection, recipient_id)
            if before is None:
                raise WarehouseDeliveryError("Recipient not found")
            cursor = connection.execute(
                """
                UPDATE warehouse_report_recipients
                SET title=?, updated_at_utc=?, revision=revision+1
                WHERE id=? AND revision=?
                """,
                (title, now, recipient_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise WarehouseRecipientConflict("Recipient revision conflict")
            after = self._recipient(connection, recipient_id)
            assert after is not None
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                event_type="warehouse_report_recipient_updated",
                entity_type="warehouse_report_recipient",
                entity_id=recipient_id,
                occurred_at_utc=now,
                before={"title": before["title"], "revision": before["revision"]},
                after={"title": after["title"], "revision": after["revision"]},
                correlation_id=f"report-recipient:{recipient_id}",
            )
            return after

    def set_active(
        self,
        recipient_id: int,
        *,
        is_active: bool,
        expected_revision: int,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            authorize()
            before = self._recipient(connection, recipient_id)
            if before is None:
                raise WarehouseDeliveryError("Recipient not found")
            cursor = connection.execute(
                """
                UPDATE warehouse_report_recipients
                SET is_active=?, updated_at_utc=?, revision=revision+1
                WHERE id=? AND revision=?
                """,
                (int(is_active), now, recipient_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise WarehouseRecipientConflict("Recipient revision conflict")
            after = self._recipient(connection, recipient_id)
            assert after is not None
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                event_type=(
                    "warehouse_report_recipient_activated"
                    if is_active else "warehouse_report_recipient_deactivated"
                ),
                entity_type="warehouse_report_recipient",
                entity_id=recipient_id,
                occurred_at_utc=now,
                before={"is_active": before["is_active"], "revision": before["revision"]},
                after={"is_active": after["is_active"], "revision": after["revision"]},
                correlation_id=f"report-recipient:{recipient_id}",
            )
            return after

    @classmethod
    def _delivery(cls, connection: sqlite3.Connection, delivery_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT d.*, r.chat_id, r.title AS recipient_title,
                   r.chat_type, r.is_active AS recipient_is_active,
                   wr.shift_id, wr.status AS report_status
            FROM warehouse_report_deliveries d
            JOIN warehouse_report_recipients r ON r.id=d.recipient_id
            JOIN warehouse_reports wr ON wr.id=d.report_id
            WHERE d.id=?
            """,
            (delivery_id,),
        ).fetchone()
        return cls._dict(row) if row is not None else None

    def prepare_deliveries(
        self,
        report_id: int,
        *,
        actor_user_id: int,
        now: str,
        stale_before: str,
        retry_failed: bool,
        authorize: Callable[[], None],
        recipient_ids: tuple[int, ...] | None = None,
    ) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            authorize()
            report = connection.execute(
                "SELECT id, shift_id, status FROM warehouse_reports WHERE id=?",
                (report_id,),
            ).fetchone()
            if report is None or report["status"] != "ready":
                raise WarehouseDeliveryError("Report is not ready")
            recipient_filter = ""
            params: list[Any] = [report_id, now, now]
            if recipient_ids is not None:
                if not recipient_ids:
                    return []
                recipient_filter = " AND id IN (" + ",".join("?" for _ in recipient_ids) + ")"
                params.extend(recipient_ids)
            connection.execute(
                """
                INSERT OR IGNORE INTO warehouse_report_deliveries (
                    report_id, recipient_id, status, attempt_count,
                    created_at_utc, updated_at_utc
                )
                SELECT ?, id, 'pending', 0, ?, ?
                FROM warehouse_report_recipients WHERE is_active=1""" + recipient_filter,
                params,
            )
            if retry_failed:
                stale = connection.execute(
                    """
                    SELECT id, attempt_count FROM warehouse_report_deliveries
                    WHERE report_id=? AND status='sending'
                      AND last_attempt_at_utc < ?
                    """,
                    (report_id, stale_before),
                ).fetchall()
                for row in stale:
                    connection.execute(
                        """
                        UPDATE warehouse_report_deliveries
                        SET status='failed', last_error_code='stale_sending',
                            last_error_text='Предыдущая отправка не была завершена',
                            updated_at_utc=? WHERE id=? AND status='sending'
                        """,
                        (now, row["id"]),
                    )
                    self._audit(
                        connection, actor_user_id=actor_user_id,
                        event_type="warehouse_report_delivery_failed",
                        entity_type="warehouse_report_delivery",
                        entity_id=int(row["id"]), occurred_at_utc=now,
                        before={"status": "sending", "attempt_count": row["attempt_count"]},
                        after={"status": "failed", "error_code": "stale_sending"},
                        correlation_id=f"report-delivery:{row['id']}",
                        shift_id=int(report["shift_id"]),
                    )
                self._audit(
                    connection, actor_user_id=actor_user_id,
                    event_type="warehouse_report_delivery_retry_requested",
                    entity_type="warehouse_report", entity_id=report_id,
                    occurred_at_utc=now, before=None,
                    after={"retryable": True},
                    correlation_id=f"report:{report_id}",
                    shift_id=int(report["shift_id"]),
                )
            statuses = ("pending", "failed") if retry_failed else ("pending",)
            placeholders = ",".join("?" for _ in statuses)
            ids_filter = ""
            query_params: list[Any] = [report_id, *statuses]
            if recipient_ids is not None:
                ids_filter = " AND d.recipient_id IN (" + ",".join("?" for _ in recipient_ids) + ")"
                query_params.extend(recipient_ids)
            rows = connection.execute(
                f"""
                SELECT d.*, r.chat_id, r.title AS recipient_title, r.chat_type
                FROM warehouse_report_deliveries d
                JOIN warehouse_report_recipients r ON r.id=d.recipient_id
                WHERE d.report_id=? AND r.is_active=1
                  AND d.status IN ({placeholders})
                  {ids_filter}
                ORDER BY d.id
                """,
                query_params,
            ).fetchall()
            return [self._dict(row) for row in rows]

    def claim_delivery(
        self,
        delivery_id: int,
        *,
        allow_failed: bool,
        actor_user_id: int,
        now: str,
        authorize: Callable[[], None],
    ) -> dict[str, Any] | None:
        with self._transaction() as connection:
            authorize()
            before = self._delivery(connection, delivery_id)
            if before is None:
                raise WarehouseDeliveryError("Delivery not found")
            allowed = {"pending"}
            if allow_failed:
                allowed.add("failed")
            if (
                before["status"] not in allowed
                or not before["recipient_is_active"]
                or before["report_status"] != "ready"
            ):
                return None
            cursor = connection.execute(
                """
                UPDATE warehouse_report_deliveries
                SET status='sending', attempt_count=attempt_count+1,
                    telegram_message_id=NULL, last_error_code=NULL,
                    last_error_text=NULL, last_attempt_at_utc=?,
                    delivered_at_utc=NULL, updated_at_utc=?
                WHERE id=? AND status=?
                """,
                (now, now, delivery_id, before["status"]),
            )
            if cursor.rowcount != 1:
                return None
            after = self._delivery(connection, delivery_id)
            assert after is not None
            self._audit(
                connection, actor_user_id=actor_user_id,
                event_type="warehouse_report_delivery_claimed",
                entity_type="warehouse_report_delivery", entity_id=delivery_id,
                occurred_at_utc=now,
                before={"status": before["status"], "attempt_count": before["attempt_count"]},
                after={"status": "sending", "attempt_count": after["attempt_count"]},
                correlation_id=f"report-delivery:{delivery_id}",
                shift_id=int(after["shift_id"]),
            )
            return after

    def mark_delivered(
        self,
        delivery_id: int,
        *,
        telegram_message_id: int,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            before = self._delivery(connection, delivery_id)
            if before is None or before["status"] != "sending":
                raise WarehouseDeliveryError("Delivery is not sending")
            cursor = connection.execute(
                """
                UPDATE warehouse_report_deliveries
                SET status='delivered', telegram_message_id=?,
                    last_error_code=NULL, last_error_text=NULL,
                    delivered_at_utc=?, updated_at_utc=?
                WHERE id=? AND status='sending'
                """,
                (telegram_message_id, now, now, delivery_id),
            )
            if cursor.rowcount != 1:
                raise WarehouseDeliveryError("Delivery state changed")
            after = self._delivery(connection, delivery_id)
            assert after is not None
            self._audit(
                connection, actor_user_id=actor_user_id,
                event_type="warehouse_report_delivery_succeeded",
                entity_type="warehouse_report_delivery", entity_id=delivery_id,
                occurred_at_utc=now,
                before={"status": "sending", "attempt_count": before["attempt_count"]},
                after={"status": "delivered", "telegram_message_id": telegram_message_id},
                correlation_id=f"report-delivery:{delivery_id}",
                shift_id=int(after["shift_id"]),
            )
            return after

    def mark_failed(
        self,
        delivery_id: int,
        *,
        error_code: str,
        error_text: str,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            before = self._delivery(connection, delivery_id)
            if before is None or before["status"] != "sending":
                raise WarehouseDeliveryError("Delivery is not sending")
            cursor = connection.execute(
                """
                UPDATE warehouse_report_deliveries
                SET status='failed', telegram_message_id=NULL,
                    last_error_code=?, last_error_text=?,
                    delivered_at_utc=NULL, updated_at_utc=?
                WHERE id=? AND status='sending'
                """,
                (error_code[:100], error_text[:500], now, delivery_id),
            )
            if cursor.rowcount != 1:
                raise WarehouseDeliveryError("Delivery state changed")
            after = self._delivery(connection, delivery_id)
            assert after is not None
            self._audit(
                connection, actor_user_id=actor_user_id,
                event_type="warehouse_report_delivery_failed",
                entity_type="warehouse_report_delivery", entity_id=delivery_id,
                occurred_at_utc=now,
                before={"status": "sending", "attempt_count": before["attempt_count"]},
                after={"status": "failed", "error_code": error_code[:100]},
                correlation_id=f"report-delivery:{delivery_id}",
                shift_id=int(after["shift_id"]),
            )
            return after

    def deliveries_for_report(self, report_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT d.*, r.chat_id, r.title AS recipient_title, r.chat_type,
                       wr.shift_id, s.closed_by
                FROM warehouse_report_deliveries d
                JOIN warehouse_report_recipients r ON r.id=d.recipient_id
                JOIN warehouse_reports wr ON wr.id=d.report_id
                JOIN warehouse_shifts s ON s.id=wr.shift_id
                WHERE d.report_id=? ORDER BY r.title COLLATE NOCASE, d.id
                """,
                (report_id,),
            ).fetchall()
            return [self._dict(row) for row in rows]
