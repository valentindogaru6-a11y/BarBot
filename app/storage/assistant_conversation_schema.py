"""Schema v13 for bounded persistent assistant conversations."""

from __future__ import annotations

import re
import sqlite3


class AssistantConversationSchemaError(RuntimeError):
    """Raised when the persistent assistant schema is incomplete or altered."""


_TABLES = {"assistant_conversation_sessions", "assistant_conversation_turns"}


def create_assistant_conversation_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE assistant_conversation_sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active')),
            started_at_utc TEXT NOT NULL,
            last_activity_at_utc TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL,
            summary_text TEXT NOT NULL DEFAULT ''
                CHECK (length(summary_text) <= 2000),
            current_topic TEXT NOT NULL DEFAULT ''
                CHECK (length(current_topic) <= 500),
            current_intent TEXT NOT NULL DEFAULT ''
                CHECK (length(current_intent) <= 100),
            last_requested_action TEXT NOT NULL DEFAULT ''
                CHECK (length(last_requested_action) <= 100),
            last_catalog_query TEXT NOT NULL DEFAULT ''
                CHECK (length(last_catalog_query) <= 500),
            last_entity_type TEXT NOT NULL DEFAULT ''
                CHECK (length(last_entity_type) <= 100),
            last_entity_id TEXT NOT NULL DEFAULT ''
                CHECK (length(last_entity_id) <= 300),
            last_entity_label TEXT NOT NULL DEFAULT ''
                CHECK (length(last_entity_label) <= 500),
            last_candidate_ids_json TEXT NOT NULL DEFAULT '[]'
                CHECK (length(last_candidate_ids_json) <= 2000),
            last_tool_name TEXT NOT NULL DEFAULT ''
                CHECK (length(last_tool_name) <= 100),
            pending_clarification TEXT NOT NULL DEFAULT ''
                CHECK (length(pending_clarification) <= 500),
            authorized_action TEXT NOT NULL DEFAULT ''
                CHECK (length(authorized_action) <= 100),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            UNIQUE (user_id, chat_id)
        );

        CREATE INDEX idx_assistant_sessions_expiry
            ON assistant_conversation_sessions(expires_at_utc, id);

        CREATE TABLE assistant_conversation_turns (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL
                REFERENCES assistant_conversation_sessions(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL CHECK (
                length(content) BETWEEN 1 AND 12000
            ),
            created_at_utc TEXT NOT NULL,
            UNIQUE (session_id, sequence)
        );

        CREATE INDEX idx_assistant_turns_session_sequence
            ON assistant_conversation_turns(session_id, sequence);
        """
    )


def assistant_conversation_schema_is_absent(
    connection: sqlite3.Connection,
) -> bool:
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'assistant_conversation_%'"
        )
    }
    return not names


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _signature(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def validate_assistant_conversation_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'assistant_conversation_%'"
        )
    }
    if tables != _TABLES:
        raise AssistantConversationSchemaError("Assistant conversation tables are incomplete")
    expected_sessions = (
        "id", "user_id", "chat_id", "status", "started_at_utc",
        "last_activity_at_utc", "expires_at_utc", "summary_text",
        "current_topic", "current_intent", "last_requested_action",
        "last_catalog_query", "last_entity_type", "last_entity_id",
        "last_entity_label", "last_candidate_ids_json", "last_tool_name",
        "pending_clarification", "authorized_action", "revision",
    )
    expected_turns = (
        "id", "session_id", "sequence", "role", "content", "created_at_utc",
    )
    if _columns(connection, "assistant_conversation_sessions") != expected_sessions:
        raise AssistantConversationSchemaError("Assistant session columns do not match v13")
    if _columns(connection, "assistant_conversation_turns") != expected_turns:
        raise AssistantConversationSchemaError("Assistant turn columns do not match v13")
    session_signature = _signature(connection, "assistant_conversation_sessions")
    turn_signature = _signature(connection, "assistant_conversation_turns")
    if any(column_type not in {"INTEGER", "TEXT"} for _, column_type, _, _ in (*session_signature, *turn_signature)):
        raise AssistantConversationSchemaError("Assistant column types do not match v13")
    if session_signature[0] != ("id", "INTEGER", 0, 1) or turn_signature[0] != ("id", "INTEGER", 0, 1):
        raise AssistantConversationSchemaError("Assistant primary keys do not match v13")
    if any(not_null != 1 for _, _, not_null, primary_key in session_signature[1:] if not primary_key):
        raise AssistantConversationSchemaError("Assistant session nullability does not match v13")
    if any(not_null != 1 for _, _, not_null, primary_key in turn_signature[1:] if not primary_key):
        raise AssistantConversationSchemaError("Assistant turn nullability does not match v13")
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_assistant_%'"
        )
    }
    if indexes != {"idx_assistant_sessions_expiry", "idx_assistant_turns_session_sequence"}:
        raise AssistantConversationSchemaError("Assistant conversation indexes do not match v13")
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(assistant_conversation_turns)"
    ).fetchall()
    if len(foreign_keys) != 1 or str(foreign_keys[0][2]) != "assistant_conversation_sessions" or str(foreign_keys[0][6]).upper() != "CASCADE":
        raise AssistantConversationSchemaError("Assistant turn foreign key does not match v13")
    for table, fragments in {
        "assistant_conversation_sessions": ("statusin('active')", "revision>0"),
        "assistant_conversation_turns": ("rolein('user','assistant')", "sequence>0"),
    }.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        compact = re.sub(r"\s+", "", str(row[0]).casefold()) if row else ""
        if any(fragment not in compact for fragment in fragments):
            raise AssistantConversationSchemaError(f"{table} CHECK constraints do not match v13")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise AssistantConversationSchemaError("Foreign key check failed for v13")
