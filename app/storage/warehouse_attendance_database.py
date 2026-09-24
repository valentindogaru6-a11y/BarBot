"""Transactional persistence for personal warehouse-shift sessions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseOperationsError,
)


class WarehouseAttendanceError(RuntimeError):
    pass


class WarehouseAttendanceConflict(WarehouseAttendanceError):
    pass


class WarehouseAttendanceDatabase:
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
    def _timestamp(value: str, field: str) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise WarehouseAttendanceError(f"{field} must be UTC RFC3339 with Z")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise WarehouseAttendanceError(f"{field} is not valid RFC3339") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise WarehouseAttendanceError(f"{field} must be UTC")
        return parsed

    @staticmethod
    def _business_date(value: str) -> date:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise WarehouseAttendanceError("business_date is invalid") from exc
        if parsed.isoformat() != value:
            raise WarehouseAttendanceError("business_date must use YYYY-MM-DD")
        return parsed

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, *, session_id: int, shift_id: int,
        actor_user_id: int, event_type: str, now: str,
        before: dict[str, Any] | None, after: dict[str, Any] | None,
        reason: str | None = None,
    ) -> None:
        encode = lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) if value is not None else None
        connection.execute(
            """INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, before_json, after_json,
                reason_text, source_raw_text, correlation_id
            ) VALUES (?,NULL,'user',?,?,'warehouse_shift_session',?,?,?, ?,?,NULL,?)""",
            (shift_id, actor_user_id, event_type, session_id, now,
             encode(before), encode(after), reason,
             f"warehouse-attendance:{session_id}:{event_type}:{now}"),
        )

    def runtime_settings(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT s.timezone,s.cutoff_local_time,
                          l.id source_location_id,l.name source_location_name
                   FROM warehouse_settings s JOIN warehouse_locations l
                     ON l.id=s.default_source_location_id
                   WHERE s.id=1 AND l.is_active=1 AND l.can_be_source=1"""
            ).fetchone()
            if row is None:
                raise WarehouseAttendanceError("Active default source is missing")
            return self._dict(row)

    def active_for_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT a.*,l.name source_location_name
                   FROM warehouse_shift_sessions a
                   JOIN warehouse_locations l ON l.id=a.source_location_id
                   WHERE a.user_id=? AND a.status='open'""", (user_id,)
            ).fetchone()
            return self._dict(row) if row else None

    def active_for_shift(self, shift_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._dict(row) for row in connection.execute(
                """SELECT * FROM warehouse_shift_sessions
                   WHERE warehouse_shift_id=? AND status='open'
                   ORDER BY opened_at_utc,id""", (shift_id,)
            )]

    def open_session(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self._business_date(payload["business_date"])
        self._timestamp(payload["now"], "opened_at_utc")
        self._timestamp(payload["started_at_utc"], "started_at_utc")
        self._timestamp(payload["scheduled_close_at_utc"], "scheduled_close_at_utc")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE user_id=? AND status='open'",
                (payload["user_id"],),
            ).fetchone()
            if existing is not None:
                return self._dict(existing), False
            try:
                shift_id = WarehouseOperationsDatabase._ensure_open_shift(connection, payload)
            except WarehouseOperationsError as exc:
                raise WarehouseAttendanceError(str(exc)) from exc
            cursor = connection.execute(
                """INSERT INTO warehouse_shift_sessions (
                    warehouse_shift_id,source_location_id,business_date,user_id,
                    opened_at_utc,closed_at_utc,opened_by_user_id,closed_by_user_id,
                    close_reason,status,display_name_snapshot,username_snapshot,
                    created_at_utc,updated_at_utc,revision
                ) VALUES (?,?,?,?,?,NULL,?,NULL,NULL,'open',?,?,?, ?,1)""",
                (shift_id,payload["source_location_id"],payload["business_date"],
                 payload["user_id"],payload["now"],payload["actor_user_id"],
                 payload["display_name"],payload.get("username"),payload["now"],payload["now"]),
            )
            session_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE id=?", (session_id,)
            ).fetchone()
            assert row is not None
            result = self._dict(row)
            self._audit(connection, session_id=session_id, shift_id=shift_id,
                        actor_user_id=payload["actor_user_id"],
                        event_type="warehouse_attendance_opened", now=payload["now"],
                        before=None, after=result)
            return result, True

    def close_own(self, user_id: int, now: str) -> tuple[dict[str, Any] | None, bool]:
        closed = self._timestamp(now, "closed_at_utc")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE user_id=? AND status='open'",
                (user_id,),
            ).fetchone()
            if row is None:
                latest = connection.execute(
                    "SELECT * FROM warehouse_shift_sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                return (self._dict(latest) if latest else None), False
            before = self._dict(row)
            if closed < self._timestamp(str(row["opened_at_utc"]), "opened_at_utc"):
                raise WarehouseAttendanceError("closed_at_utc precedes opened_at_utc")
            cursor = connection.execute(
                """UPDATE warehouse_shift_sessions SET status='closed',closed_at_utc=?,
                   closed_by_user_id=?,updated_at_utc=?,revision=revision+1
                   WHERE id=? AND status='open' AND revision=?""",
                (now,user_id,now,row["id"],row["revision"]),
            )
            if cursor.rowcount != 1:
                raise WarehouseAttendanceConflict("Attendance revision conflict")
            updated = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE id=?", (row["id"],)
            ).fetchone()
            assert updated is not None
            result = self._dict(updated)
            self._audit(connection, session_id=int(row["id"]),
                        shift_id=int(row["warehouse_shift_id"]), actor_user_id=user_id,
                        event_type="warehouse_attendance_closed", now=now,
                        before=before, after=result)
            return result, True

    def force_close(self, session_id: int, actor_user_id: int, reason: str,
                    now: str, expected_revision: int) -> dict[str, Any]:
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise ValueError("Причина должна содержать от 1 до 500 символов")
        closed = self._timestamp(now, "closed_at_utc")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or row["status"] != "open":
                raise WarehouseAttendanceConflict("Session is no longer open")
            if int(row["revision"]) != expected_revision:
                raise WarehouseAttendanceConflict("Attendance revision conflict")
            before = self._dict(row)
            if closed < self._timestamp(str(row["opened_at_utc"]), "opened_at_utc"):
                raise WarehouseAttendanceError("closed_at_utc precedes opened_at_utc")
            cursor = connection.execute(
                """UPDATE warehouse_shift_sessions SET status='force_closed',
                   closed_at_utc=?,closed_by_user_id=?,close_reason=?,updated_at_utc=?,
                   revision=revision+1 WHERE id=? AND status='open' AND revision=?""",
                (now,actor_user_id,reason,now,session_id,expected_revision),
            )
            if cursor.rowcount != 1:
                raise WarehouseAttendanceConflict("Attendance revision conflict")
            updated = connection.execute(
                "SELECT * FROM warehouse_shift_sessions WHERE id=?", (session_id,)
            ).fetchone()
            assert updated is not None
            result = self._dict(updated)
            self._audit(connection, session_id=session_id,
                        shift_id=int(row["warehouse_shift_id"]), actor_user_id=actor_user_id,
                        event_type="warehouse_attendance_force_closed", now=now,
                        before=before, after=result, reason=reason)
            return result

    def open_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._dict(row) for row in connection.execute(
                """SELECT a.*,l.name source_location_name
                   FROM warehouse_shift_sessions a
                   JOIN warehouse_locations l ON l.id=a.source_location_id
                   WHERE a.status='open' ORDER BY a.opened_at_utc,a.id"""
            )]

    def sessions_between(self, first_date: str, last_date: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._dict(row) for row in connection.execute(
                """SELECT a.*,l.name source_location_name,u.role user_role,
                          opener.first_name opener_first_name,opener.last_name opener_last_name,
                          closer.first_name closer_first_name,closer.last_name closer_last_name
                   FROM warehouse_shift_sessions a
                   JOIN warehouse_locations l ON l.id=a.source_location_id
                   LEFT JOIN users u ON u.telegram_user_id=a.user_id
                   LEFT JOIN users opener ON opener.telegram_user_id=a.opened_by_user_id
                   LEFT JOIN users closer ON closer.telegram_user_id=a.closed_by_user_id
                   WHERE a.business_date BETWEEN ? AND ? ORDER BY a.business_date,a.opened_at_utc,a.id""",
                (first_date,last_date),
            )]
