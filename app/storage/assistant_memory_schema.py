"""Schema v15 for owner-confirmed, revisioned assistant memories."""

from __future__ import annotations

import re
import sqlite3


class AssistantMemorySchemaError(RuntimeError):
    pass


SCHEMA_STATEMENTS = (
    """CREATE TABLE assistant_long_term_memories (
        id INTEGER PRIMARY KEY,
        opaque_token TEXT NOT NULL UNIQUE CHECK(
            length(opaque_token) BETWEEN 16 AND 32
            AND opaque_token NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        owner_user_id INTEGER NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('user','bar')),
        kind TEXT NOT NULL CHECK(kind IN (
            'terminology','product_alias','user_preference',
            'workflow_preference','standing_instruction','business_fact'
        )),
        normalized_content TEXT NOT NULL CHECK(length(normalized_content) BETWEEN 1 AND 2000),
        source TEXT NOT NULL CHECK(source IN ('explicit_remember','confirmed_preview')),
        status TEXT NOT NULL CHECK(status IN ('pending','active','superseded','deleted','cancelled')),
        supersedes_id INTEGER NULL REFERENCES assistant_long_term_memories(id),
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        CHECK(expires_at_utc IS NULL OR expires_at_utc>created_at_utc),
        CHECK((status='active') OR updated_at_utc>=created_at_utc)
    )""",
    """CREATE INDEX ix_assistant_memory_owner_active
       ON assistant_long_term_memories(owner_user_id,scope,status,kind,id)""",
    """CREATE UNIQUE INDEX ux_assistant_memory_active_content
       ON assistant_long_term_memories(owner_user_id,scope,kind,normalized_content)
       WHERE status='active'""",
)


def create_assistant_memory_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def assistant_memory_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='assistant_long_term_memories'"
    ).fetchone() is None


def validate_assistant_memory_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='assistant_long_term_memories'"
    ).fetchone()
    if row is None:
        raise AssistantMemorySchemaError("Assistant memory table is missing")
    columns = tuple(
        str(value[1])
        for value in connection.execute("PRAGMA table_info(assistant_long_term_memories)")
    )
    expected = (
        "id", "opaque_token", "owner_user_id", "scope", "kind",
        "normalized_content", "source", "status", "supersedes_id",
        "created_at_utc", "updated_at_utc", "expires_at_utc", "revision",
    )
    if columns != expected:
        raise AssistantMemorySchemaError("Assistant memory columns do not match v15")
    indexes = {
        str(value[1])
        for value in connection.execute("PRAGMA index_list(assistant_long_term_memories)")
    }
    if not {"ix_assistant_memory_owner_active", "ux_assistant_memory_active_content"} <= indexes:
        raise AssistantMemorySchemaError("Assistant memory indexes are incomplete")
    compact = re.sub(r"\s+", "", str(row[0]).casefold())
    for fragment in (
        "scopein('user','bar')", "statusin('pending','active','superseded','deleted','cancelled')",
        "sourcein('explicit_remember','confirmed_preview')", "revision>0",
    ):
        if fragment not in compact:
            raise AssistantMemorySchemaError("Assistant memory constraints do not match v15")
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(assistant_long_term_memories)"
    ).fetchall()
    if len(foreign_keys) != 1 or str(foreign_keys[0][2]) != "assistant_long_term_memories":
        raise AssistantMemorySchemaError("Assistant memory foreign key is invalid")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise AssistantMemorySchemaError("Foreign key check failed for v15")
