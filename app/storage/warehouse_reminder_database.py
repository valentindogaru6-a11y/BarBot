"""SQLite settings, event creation and atomic delivery claims for reminders."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class WarehouseReminderError(RuntimeError):
    pass


class WarehouseReminderConflict(WarehouseReminderError):
    pass


class WarehouseReminderDatabase:
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
    def _audit(connection: sqlite3.Connection, *, shift_id: int | None, actor_user_id: int | None, event_type: str, entity_id: int, now: str, after: dict[str, Any] | None = None) -> None:
        connection.execute(
            """INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, before_json, after_json,
                reason_text, source_raw_text, correlation_id
            ) VALUES (?, NULL, ?, ?, ?, 'warehouse_reminder', ?, ?, NULL, ?,
                      NULL, NULL, ?)""",
            (shift_id, "user" if actor_user_id is not None else "system", actor_user_id,
             event_type, entity_id, now,
             json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if after is not None else None,
             f"warehouse-reminder:{entity_id}:{event_type}:{now}"),
        )

    def settings(self, source_location_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?", (source_location_id,)).fetchone()
            return dict(row) if row is not None else None

    def save_settings(self, source_location_id: int, values: dict[str, Any], expected_revision: int | None, now: str, *, actor_user_id: int | None = None) -> dict[str, Any]:
        with self._transaction() as connection:
            old = connection.execute("SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?", (source_location_id,)).fetchone()
            if old is None:
                if expected_revision not in (None, 0):
                    raise WarehouseReminderConflict("Settings revision conflict")
                connection.execute(
                    """INSERT INTO warehouse_reminder_settings (
                        source_location_id, enabled, target_chat_id, target_title, timezone,
                        first_reminder_minutes_before_close, repeat_interval_minutes,
                        max_reminders, revision, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (source_location_id, values["enabled"], values["target_chat_id"], values["target_title"], values["timezone"], values["first"], values["repeat"], values["maximum"], now, now),
                )
                created = True
            else:
                if expected_revision != old["revision"]:
                    raise WarehouseReminderConflict("Settings revision conflict")
                cursor = connection.execute(
                    """UPDATE warehouse_reminder_settings
                    SET enabled=?, target_chat_id=?, target_title=?, timezone=?,
                        first_reminder_minutes_before_close=?, repeat_interval_minutes=?,
                        max_reminders=?, revision=revision+1, updated_at_utc=?
                    WHERE source_location_id=? AND revision=?""",
                    (values["enabled"], values["target_chat_id"], values["target_title"], values["timezone"], values["first"], values["repeat"], values["maximum"], now, source_location_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise WarehouseReminderConflict("Settings revision conflict")
                created = False
            self._audit(connection, shift_id=None, actor_user_id=actor_user_id, event_type="warehouse_reminder_settings_updated", entity_id=source_location_id, now=now, after={"enabled": bool(values["enabled"]), "created": created})
            if (old is None and values["enabled"]) or (old is not None and bool(old["enabled"]) != bool(values["enabled"])):
                self._audit(connection, shift_id=None, actor_user_id=actor_user_id, event_type="warehouse_reminder_enabled" if values["enabled"] else "warehouse_reminder_disabled", entity_id=source_location_id, now=now)
            if (old is None and values["target_chat_id"] is not None) or (old is not None and old["target_chat_id"] != values["target_chat_id"]):
                self._audit(connection, shift_id=None, actor_user_id=actor_user_id, event_type="warehouse_reminder_target_changed", entity_id=source_location_id, now=now)
            row = connection.execute("SELECT * FROM warehouse_reminder_settings WHERE source_location_id=?", (source_location_id,)).fetchone()
            assert row is not None
            return dict(row)

    def open_shifts(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT s.*, x.* FROM warehouse_shifts s
                JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id
                WHERE x.enabled=1 AND s.status IN ('open', 'waiting_required')"""
            ).fetchall()]

    def ensure_events(self, shift_id: int, schedules: list[tuple[int, str]], now: str) -> None:
        with self._transaction() as connection:
            for sequence, scheduled_at_utc in schedules:
                connection.execute(
                    """INSERT OR IGNORE INTO warehouse_reminder_events (
                        shift_id, sequence_number, scheduled_at_utc, status, attempt_count,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
                    (shift_id, sequence, scheduled_at_utc, now, now),
                )

    def cancel_ineligible(self, now: str) -> None:
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT e.id, e.shift_id FROM warehouse_reminder_events e
                JOIN warehouse_shifts s ON s.id=e.shift_id
                WHERE e.status IN ('pending', 'failed') AND s.status IN ('closing', 'closed')"""
            ).fetchall()
            connection.execute(
                """UPDATE warehouse_reminder_events SET status='cancelled', updated_at_utc=?
                WHERE status IN ('pending', 'failed') AND shift_id IN (
                    SELECT id FROM warehouse_shifts WHERE status IN ('closing', 'closed')
                )""", (now,)
            )
            for row in rows:
                self._audit(connection, shift_id=int(row["shift_id"]), actor_user_id=None, event_type="warehouse_reminder_cancelled", entity_id=int(row["id"]), now=now)

    def due(self, now: str, stale_before: str) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            stale_rows = connection.execute("SELECT id, shift_id FROM warehouse_reminder_events WHERE status='sending' AND claimed_at_utc<?", (stale_before,)).fetchall()
            connection.execute(
                """UPDATE warehouse_reminder_events
                SET status='failed', last_error_code='stale_sending',
                    last_error_text='Предыдущая отправка не завершена', updated_at_utc=?
                WHERE status='sending' AND claimed_at_utc<?""", (now, stale_before)
            )
            for row in stale_rows:
                self._audit(connection, shift_id=int(row["shift_id"]), actor_user_id=None, event_type="warehouse_reminder_failed", entity_id=int(row["id"]), now=now, after={"code": "stale_sending"})
            return [dict(row) for row in connection.execute(
                """SELECT e.*, s.status AS shift_status, s.business_date, x.enabled,
                           x.target_chat_id, x.target_title
                    FROM warehouse_reminder_events e
                    JOIN warehouse_shifts s ON s.id=e.shift_id
                    JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id
                    WHERE e.status IN ('pending', 'failed') AND e.scheduled_at_utc<=?
                    ORDER BY e.scheduled_at_utc, e.id""", (now,)
            ).fetchall()]

    def claim(self, event_id: int, now: str) -> dict[str, Any] | None:
        """Atomically own one event immediately before the Telegram API request."""
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT e.*, s.status AS shift_status, s.business_date, x.enabled, x.target_chat_id
                FROM warehouse_reminder_events e
                JOIN warehouse_shifts s ON s.id=e.shift_id
                JOIN warehouse_reminder_settings x ON x.source_location_id=s.source_location_id
                WHERE e.id=?""", (event_id,)
            ).fetchone()
            if row is None:
                return None
            if row["shift_status"] not in {"open", "waiting_required"}:
                cursor = connection.execute("UPDATE warehouse_reminder_events SET status='cancelled', updated_at_utc=? WHERE id=? AND status IN ('pending', 'failed')", (now, event_id))
                if cursor.rowcount:
                    self._audit(connection, shift_id=int(row["shift_id"]), actor_user_id=None, event_type="warehouse_reminder_cancelled", entity_id=event_id, now=now)
                return None
            if not row["enabled"] or row["target_chat_id"] is None or row["status"] not in {"pending", "failed"}:
                return None
            cursor = connection.execute(
                """UPDATE warehouse_reminder_events SET status='sending', attempt_count=attempt_count+1,
                    last_error_code=NULL, last_error_text=NULL, claimed_at_utc=?, updated_at_utc=?
                WHERE id=? AND status=?""", (now, now, event_id, row["status"])
            )
            if cursor.rowcount != 1:
                return None
            self._audit(connection, shift_id=int(row["shift_id"]), actor_user_id=None, event_type="warehouse_reminder_claimed", entity_id=event_id, now=now)
            event = connection.execute("SELECT * FROM warehouse_reminder_events WHERE id=?", (event_id,)).fetchone()
            assert event is not None
            claimed = dict(event)
            claimed["target_chat_id"] = row["target_chat_id"]
            claimed["business_date"] = row["business_date"]
            return claimed

    def finish(self, event_id: int, telegram_message_id: int | None, now: str, error: tuple[str, str] | None = None) -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT shift_id FROM warehouse_reminder_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                return
            if error is None:
                cursor = connection.execute("UPDATE warehouse_reminder_events SET status='sent', telegram_message_id=?, sent_at_utc=?, updated_at_utc=? WHERE id=? AND status='sending'", (telegram_message_id, now, now, event_id))
                event_type, after = "warehouse_reminder_sent", {"telegram_message_id": telegram_message_id}
            else:
                cursor = connection.execute("UPDATE warehouse_reminder_events SET status='failed', last_error_code=?, last_error_text=?, updated_at_utc=? WHERE id=? AND status='sending'", (error[0][:100], error[1][:500], now, event_id))
                event_type, after = "warehouse_reminder_failed", {"code": error[0][:100]}
            if cursor.rowcount:
                self._audit(connection, shift_id=int(row["shift_id"]), actor_user_id=None, event_type=event_type, entity_id=event_id, now=now, after=after)

    def events(self, source_location_id: int, limit: int = 12) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT e.*, s.business_date FROM warehouse_reminder_events e
                JOIN warehouse_shifts s ON s.id=e.shift_id
                WHERE s.source_location_id=? ORDER BY e.id DESC LIMIT ?""", (source_location_id, limit)
            ).fetchall()]
