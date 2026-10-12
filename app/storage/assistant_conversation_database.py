"""Transactional persistence for bounded assistant conversation state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class AssistantConversationConflict(RuntimeError):
    """Raised if an optimistic conversation update loses a race."""


class AssistantConversationDatabase:
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

    def load(self, user_id: int, chat_id: int, now: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT * FROM assistant_conversation_sessions WHERE user_id=? AND chat_id=? AND expires_at_utc>?",
                (user_id, chat_id, now),
            ).fetchone()
            if row is None:
                return None
            turns = connection.execute(
                "SELECT role,content,created_at_utc FROM assistant_conversation_turns WHERE session_id=? ORDER BY sequence",
                (row["id"],),
            ).fetchall()
            return dict(row), [dict(turn) for turn in turns]

    def save(
        self,
        user_id: int,
        chat_id: int,
        data: dict[str, Any],
        turns: list[dict[str, str]],
        *,
        expected_revision: int | None,
        now: str,
    ) -> int:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT id,revision FROM assistant_conversation_sessions WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            ).fetchone()
            values = (
                data["last_activity_at_utc"], data["expires_at_utc"],
                data.get("summary_text", ""), data.get("current_topic", ""),
                data.get("current_intent", ""), data.get("last_requested_action", ""),
                data.get("last_catalog_query", ""), data.get("last_entity_type", ""),
                data.get("last_entity_id", ""), data.get("last_entity_label", ""),
                json.dumps(data.get("candidate_ids", ()), ensure_ascii=False),
                data.get("last_tool_name", ""), data.get("pending_clarification", ""),
                data.get("authorized_action", ""),
            )
            if row is None:
                if expected_revision not in (None, 0):
                    raise AssistantConversationConflict("Conversation disappeared")
                cursor = connection.execute(
                    """INSERT INTO assistant_conversation_sessions (
                       user_id,chat_id,started_at_utc,last_activity_at_utc,expires_at_utc,
                       summary_text,current_topic,current_intent,last_requested_action,
                       last_catalog_query,last_entity_type,last_entity_id,last_entity_label,
                       last_candidate_ids_json,last_tool_name,pending_clarification,
                       authorized_action,revision
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (user_id, chat_id, data["started_at_utc"], *values),
                )
                session_id, revision = int(cursor.lastrowid), 1
            else:
                current_revision = int(row["revision"])
                if expected_revision is not None and current_revision != expected_revision:
                    raise AssistantConversationConflict("Conversation revision changed")
                cursor = connection.execute(
                    """UPDATE assistant_conversation_sessions SET
                       last_activity_at_utc=?,expires_at_utc=?,summary_text=?,current_topic=?,
                       current_intent=?,last_requested_action=?,last_catalog_query=?,
                       last_entity_type=?,last_entity_id=?,last_entity_label=?,
                       last_candidate_ids_json=?,last_tool_name=?,pending_clarification=?,
                       authorized_action=?,revision=revision+1
                       WHERE id=? AND revision=?""",
                    (*values, int(row["id"]), current_revision),
                )
                if cursor.rowcount != 1:
                    raise AssistantConversationConflict("Conversation revision changed")
                session_id, revision = int(row["id"]), current_revision + 1
            connection.execute(
                "DELETE FROM assistant_conversation_turns WHERE session_id=?", (session_id,)
            )
            connection.executemany(
                "INSERT INTO assistant_conversation_turns(session_id,sequence,role,content,created_at_utc) VALUES (?,?,?,?,?)",
                [
                    (session_id, index, turn["role"], turn["content"], turn["created_at_utc"])
                    for index, turn in enumerate(turns, 1)
                ],
            )
            return revision

    def clear(self, user_id: int, chat_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM assistant_conversation_sessions WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            )

    @staticmethod
    def _cleanup(connection: sqlite3.Connection, now: str, limit: int = 25) -> None:
        connection.execute(
            """DELETE FROM assistant_conversation_sessions WHERE id IN (
               SELECT id FROM assistant_conversation_sessions
               WHERE expires_at_utc<=? ORDER BY expires_at_utc,id LIMIT ?
            )""",
            (now, limit),
        )
