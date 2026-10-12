"""Schema v14 for restart-safe assistant action proposals."""

from __future__ import annotations

import re
import sqlite3


class AssistantActionSchemaError(RuntimeError):
    pass


SCHEMA_STATEMENTS = (
    """CREATE TABLE assistant_action_proposals (
        id INTEGER PRIMARY KEY,
        opaque_token TEXT NOT NULL UNIQUE CHECK(
            length(opaque_token) BETWEEN 16 AND 32
            AND opaque_token NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        actor_user_id INTEGER NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        capability_id TEXT NOT NULL CHECK(length(capability_id) BETWEEN 1 AND 100),
        risk_level INTEGER NOT NULL CHECK(risk_level BETWEEN 1 AND 3),
        status TEXT NOT NULL CHECK(status IN (
            'collecting','ready','confirmed','executed','cancelled','expired',
            'failed','conflicted'
        )),
        target_type TEXT NULL CHECK(target_type IS NULL OR length(target_type)<=100),
        target_id TEXT NULL CHECK(target_id IS NULL OR length(target_id)<=300),
        target_revision INTEGER NULL CHECK(target_revision IS NULL OR target_revision>=0),
        arguments_json TEXT NOT NULL CHECK(length(arguments_json)<=12000),
        preview_json TEXT NOT NULL CHECK(length(preview_json)<=12000),
        reason_text TEXT NULL CHECK(reason_text IS NULL OR length(reason_text)<=500),
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        confirmed_at_utc TEXT NULL,
        executed_at_utc TEXT NULL,
        cancelled_at_utc TEXT NULL,
        result_json TEXT NULL CHECK(result_json IS NULL OR length(result_json)<=12000),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        CHECK(expires_at_utc>created_at_utc),
        CHECK(status!='executed' OR executed_at_utc IS NOT NULL),
        CHECK(status!='cancelled' OR cancelled_at_utc IS NOT NULL)
    )""",
    """CREATE UNIQUE INDEX ux_assistant_action_actor_active
       ON assistant_action_proposals(actor_user_id)
       WHERE status IN ('collecting','ready','confirmed')""",
    """CREATE INDEX ix_assistant_action_token_status
       ON assistant_action_proposals(opaque_token,status)""",
    """CREATE INDEX ix_assistant_action_expiry
       ON assistant_action_proposals(status,expires_at_utc,id)""",
)


def create_assistant_action_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def assistant_action_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='assistant_action_proposals'"
    ).fetchone() is None


def validate_assistant_action_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='assistant_action_proposals'"
    ).fetchone()
    if row is None:
        raise AssistantActionSchemaError("Assistant action proposal table is missing")
    columns = tuple(
        str(value[1])
        for value in connection.execute("PRAGMA table_info(assistant_action_proposals)")
    )
    expected = (
        "id", "opaque_token", "actor_user_id", "source_chat_id",
        "source_message_id", "capability_id", "risk_level", "status",
        "target_type", "target_id", "target_revision", "arguments_json",
        "preview_json", "reason_text", "created_at_utc", "expires_at_utc",
        "confirmed_at_utc", "executed_at_utc", "cancelled_at_utc",
        "result_json", "revision",
    )
    if columns != expected:
        raise AssistantActionSchemaError("Assistant action proposal columns do not match v14")
    indexes = {
        str(value[1])
        for value in connection.execute("PRAGMA index_list(assistant_action_proposals)")
    }
    required_indexes = {
        "ux_assistant_action_actor_active",
        "ix_assistant_action_token_status",
        "ix_assistant_action_expiry",
    }
    if not required_indexes <= indexes:
        raise AssistantActionSchemaError("Assistant action proposal indexes are incomplete")
    compact = re.sub(r"\s+", "", str(row[0]).casefold())
    for fragment in (
        "risk_levelbetween1and3",
        "revision>0",
        "statusin('collecting','ready','confirmed','executed','cancelled','expired','failed','conflicted')",
        "status!='executed'orexecuted_at_utcisnotnull",
        "status!='cancelled'orcancelled_at_utcisnotnull",
    ):
        if fragment not in compact:
            raise AssistantActionSchemaError("Assistant action CHECK constraints do not match v14")
    index_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_assistant_action_actor_active'"
    ).fetchone()
    index_sql = re.sub(r"\s+", "", str(index_row[0]).casefold()) if index_row else ""
    if "wherestatusin('collecting','ready','confirmed')" not in index_sql:
        raise AssistantActionSchemaError("Assistant active-proposal uniqueness is incomplete")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise AssistantActionSchemaError("Foreign key check failed for v14")
