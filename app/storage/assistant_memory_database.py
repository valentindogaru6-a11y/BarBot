"""Transactional storage for explicitly confirmed assistant memories."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class AssistantMemoryConflict(RuntimeError):
    pass


class AssistantMemoryDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_active(self, owner_user_id: int, now: str, *, limit: int = 12) -> tuple[dict[str, Any], ...]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT opaque_token,scope,kind,normalized_content,source,
                          created_at_utc,updated_at_utc,expires_at_utc,revision
                   FROM assistant_long_term_memories
                   WHERE owner_user_id=? AND status='active'
                     AND (expires_at_utc IS NULL OR expires_at_utc>?)
                   ORDER BY updated_at_utc DESC,id DESC LIMIT ?""",
                (owner_user_id, now, limit),
            ).fetchall()
            return tuple(dict(row) for row in rows)
        finally:
            connection.close()

    def create(self, *, token: str, owner_user_id: int, scope: str, kind: str,
               content: str, source: str, now: str, expires_at: str | None = None,
               supersedes_token: str | None = None, status: str = "active") -> dict[str, Any]:
        with self._transaction() as connection:
            supersedes_id = None
            if supersedes_token:
                previous = connection.execute(
                    "SELECT id,revision FROM assistant_long_term_memories WHERE opaque_token=? AND owner_user_id=? AND status='active'",
                    (supersedes_token, owner_user_id),
                ).fetchone()
                if previous is None:
                    raise AssistantMemoryConflict("Memory target changed")
                supersedes_id = int(previous["id"])
                connection.execute(
                    "UPDATE assistant_long_term_memories SET status='superseded',updated_at_utc=?,revision=revision+1 WHERE id=? AND revision=?",
                    (now, supersedes_id, int(previous["revision"])),
                )
            try:
                connection.execute(
                    """INSERT INTO assistant_long_term_memories(
                       opaque_token,owner_user_id,scope,kind,normalized_content,source,
                       status,supersedes_id,created_at_utc,updated_at_utc,expires_at_utc,revision
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (token, owner_user_id, scope, kind, content, source,
                     status, supersedes_id, now, now, expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise AssistantMemoryConflict("Memory already exists or changed") from exc
            row = connection.execute(
                "SELECT * FROM assistant_long_term_memories WHERE opaque_token=?", (token,)
            ).fetchone()
            return dict(row)

    def get_owned(self, owner_user_id: int, token: str) -> dict[str, Any] | None:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM assistant_long_term_memories WHERE owner_user_id=? AND opaque_token=?",
                (owner_user_id, token),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def transition(self, owner_user_id: int, token: str, *, expected_revision: int,
                   expected_status: str, status: str, now: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM assistant_long_term_memories WHERE owner_user_id=? AND opaque_token=?",
                (owner_user_id, token),
            ).fetchone()
            if row is None:
                raise AssistantMemoryConflict("Memory preview not found")
            if str(row["status"]) == status:
                return dict(row)
            if str(row["status"]) != expected_status or int(row["revision"]) != expected_revision:
                raise AssistantMemoryConflict("Memory preview changed")
            if row["expires_at_utc"] is not None and str(row["expires_at_utc"]) <= now:
                raise AssistantMemoryConflict("Memory preview expired")
            try:
                cursor = connection.execute(
                    "UPDATE assistant_long_term_memories SET status=?,updated_at_utc=?,expires_at_utc=CASE WHEN ?='active' THEN NULL ELSE expires_at_utc END,revision=revision+1 WHERE owner_user_id=? AND opaque_token=? AND status=? AND revision=?",
                    (status, now, status, owner_user_id, token, expected_status, expected_revision),
                )
            except sqlite3.IntegrityError as exc:
                raise AssistantMemoryConflict("Memory already exists") from exc
            if cursor.rowcount != 1:
                raise AssistantMemoryConflict("Memory preview changed")
            updated = connection.execute(
                "SELECT * FROM assistant_long_term_memories WHERE opaque_token=?", (token,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def delete(self, owner_user_id: int, token: str, *, expected_revision: int, now: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE assistant_long_term_memories
                   SET status='deleted',updated_at_utc=?,revision=revision+1
                   WHERE opaque_token=? AND owner_user_id=? AND status='active' AND revision=?""",
                (now, token, owner_user_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise AssistantMemoryConflict("Memory target changed")
            return True
