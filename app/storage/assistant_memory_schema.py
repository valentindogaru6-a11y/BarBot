"""Schema v15 for owner-confirmed, revisioned assistant memories."""

from __future__ import annotations

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
        owner_user_id INTEGER NOT NULL CHECK(owner_user_id>0),
        source_chat_id INTEGER NOT NULL CHECK(source_chat_id>0),
        scope TEXT NOT NULL CHECK(scope IN ('user','bar')),
        kind TEXT NOT NULL CHECK(kind IN (
            'terminology','product_alias','user_preference',
            'workflow_preference','standing_instruction','business_fact'
        )),
        normalized_content TEXT NOT NULL CHECK(length(normalized_content) BETWEEN 1 AND 2000),
        source TEXT NOT NULL CHECK(source IN ('explicit_remember','confirmed_preview')),
        status TEXT NOT NULL CHECK(status IN ('pending','active','superseded','deleted','cancelled')),
        supersedes_id INTEGER NULL,
        created_at_utc TEXT NOT NULL CHECK(julianday(created_at_utc) IS NOT NULL),
        updated_at_utc TEXT NOT NULL CHECK(julianday(updated_at_utc) IS NOT NULL),
        expires_at_utc TEXT NULL CHECK(expires_at_utc IS NULL OR julianday(expires_at_utc) IS NOT NULL),
        confirmation_expires_at_utc TEXT NULL CHECK(confirmation_expires_at_utc IS NULL OR (julianday(confirmation_expires_at_utc) IS NOT NULL AND julianday(confirmation_expires_at_utc)>julianday(created_at_utc))),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        UNIQUE(id,owner_user_id,source_chat_id),
        FOREIGN KEY(supersedes_id,owner_user_id,source_chat_id)
            REFERENCES assistant_long_term_memories(id,owner_user_id,source_chat_id),
        CHECK(expires_at_utc IS NULL OR julianday(expires_at_utc)>julianday(created_at_utc)),
        CHECK(julianday(updated_at_utc)>=julianday(created_at_utc)),
        CHECK(status!='pending' OR (expires_at_utc IS NOT NULL AND confirmation_expires_at_utc IS NOT NULL))
    )""",
    """CREATE INDEX ix_assistant_memory_owner_active
       ON assistant_long_term_memories(owner_user_id,source_chat_id,scope,status,kind,id)""",
    """CREATE UNIQUE INDEX ux_assistant_memory_active_content
       ON assistant_long_term_memories(owner_user_id,source_chat_id,scope,kind,normalized_content)
       WHERE status='active'""",
    """CREATE TABLE assistant_memory_delete_previews (
        id INTEGER PRIMARY KEY,
        opaque_token TEXT NOT NULL UNIQUE CHECK(
            length(opaque_token) BETWEEN 16 AND 32
            AND opaque_token NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        memory_id INTEGER NOT NULL,
        owner_user_id INTEGER NOT NULL CHECK(owner_user_id>0),
        source_chat_id INTEGER NOT NULL CHECK(source_chat_id>0),
        expected_revision INTEGER NOT NULL CHECK(expected_revision>0),
        status TEXT NOT NULL CHECK(status IN ('pending','deleted','cancelled')),
        created_at_utc TEXT NOT NULL CHECK(julianday(created_at_utc) IS NOT NULL),
        expires_at_utc TEXT NOT NULL CHECK(julianday(expires_at_utc) IS NOT NULL),
        CHECK(julianday(expires_at_utc)>julianday(created_at_utc)),
        FOREIGN KEY(memory_id,owner_user_id,source_chat_id)
            REFERENCES assistant_long_term_memories(id,owner_user_id,source_chat_id)
            ON UPDATE NO ACTION ON DELETE CASCADE
    )""",
)


def create_assistant_memory_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def assistant_memory_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name IN "
        "('assistant_long_term_memories','assistant_memory_delete_previews')"
    ).fetchone() is None


SqlToken = tuple[str, str]
_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def _tokens(sql: str) -> tuple[SqlToken, ...]:
    """Lex DDL without interpreting quoted text or comments as SQL tokens."""
    result: list[SqlToken] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
        elif sql.startswith('--', index):
            end = sql.find('\n', index + 2)
            index = len(sql) if end < 0 else end + 1
        elif sql.startswith('/*', index):
            end = sql.find('*/', index + 2)
            if end < 0:
                raise AssistantMemorySchemaError('Unterminated DDL comment')
            index = end + 2
        elif char in "'\"`[":
            closing = ']' if char == '[' else char
            kind = 'literal' if char == "'" else 'identifier'
            value: list[str] = []
            index += 1
            while index < len(sql):
                if sql[index] == closing:
                    # SQLite escapes quote/backtick delimiters by doubling;
                    # bracket identifiers end at the first closing bracket.
                    if closing != ']' and sql[index:index + 2] == closing * 2:
                        value.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise AssistantMemorySchemaError('Unterminated quoted DDL token')
            result.append((kind, ''.join(value)))
        elif char.isalnum() or char in '_$' or ord(char) >= 128:
            start = index
            while index < len(sql) and (
                sql[index].isalnum() or sql[index] in '_$' or ord(sql[index]) >= 128
            ):
                index += 1
            result.append(('word', sql[start:index].translate(_ASCII_LOWER)))
        else:
            result.append(('symbol', char))
            index += 1
    return tuple(result)


def _normalized(tokens: tuple[SqlToken, ...]) -> tuple[SqlToken, ...]:
    # Identifier quoting/case is immaterial, but literals remain distinct and
    # byte-exact. Token boundaries are retained ("a b" never becomes "ab").
    # SQLite folds ASCII identifiers only; Unicode casefold could equate a
    # distinct quoted name (and SQLite's fallback string literal) with a column.
    return tuple(('word', value.translate(_ASCII_LOWER)) if kind == 'identifier' else (kind, value)
                 for kind, value in tokens)


def _expression(tokens: tuple[SqlToken, ...]) -> tuple[SqlToken, ...]:
    """Strip whole-expression parentheses in O(n) time/space, without recursion."""
    matching_close = [-1] * len(tokens)
    opened: list[int] = []
    for index, token in enumerate(tokens):
        if token == ('symbol', '('):
            opened.append(index)
        elif token == ('symbol', ')'):
            if not opened:
                raise AssistantMemorySchemaError('Unbalanced CHECK expression')
            matching_close[opened.pop()] = index
    if opened:
        raise AssistantMemorySchemaError('Unbalanced CHECK expression')

    left, right = 0, len(tokens) - 1
    while left <= right and tokens[left] == ('symbol', '(') and matching_close[left] == right:
        left += 1
        right -= 1
    if left > right:
        raise AssistantMemorySchemaError('Empty CHECK expression')
    return _normalized(tokens[left:right + 1])


def _checks(sql: str) -> tuple[tuple[SqlToken, ...], ...]:
    tokens = _tokens(sql)
    result: list[tuple[SqlToken, ...]] = []
    index = 0
    while index < len(tokens) - 1:
        if tokens[index:index + 2] != (('word', 'check'), ('symbol', '(')):
            index += 1
            continue
        start = index + 2
        depth, index = 1, start
        while index < len(tokens) and depth:
            depth += (tokens[index] == ('symbol', '(')) - (tokens[index] == ('symbol', ')'))
            index += 1
        if depth or index == start + 1:
            raise AssistantMemorySchemaError('Invalid memory CHECK expression')
        result.append(_expression(tokens[start:index - 1]))
    return tuple(sorted(result))


def _table_signature(connection: sqlite3.Connection, name: str) -> tuple:
    info = tuple(tuple(row[1:]) for row in connection.execute(f'PRAGMA table_info("{name}")'))
    fks = tuple(sorted(tuple(row[1:]) for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')))
    indexes = []
    for row in connection.execute(f'PRAGMA index_list("{name}")'):
        index_name = str(row[1])
        columns = tuple(r[2] for r in connection.execute(f'PRAGMA index_info("{index_name}")'))
        layout = tuple(tuple(r[2:]) for r in connection.execute(f'PRAGMA index_xinfo("{index_name}")'))
        sql_row = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (index_name,)).fetchone()
        identity = index_name if row[3] == 'c' else str(row[3])
        predicate = ()
        if row[4] and sql_row and sql_row[0]:
            tokens = _tokens(sql_row[0])
            where = tokens.index(('word', 'where'))
            predicate = _expression(tokens[where + 1:])
        indexes.append((identity, int(row[2]), int(row[4]), columns, layout, predicate))
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return info, fks, tuple(sorted(indexes)), _checks(str(row[0])) if row else ()


def validate_assistant_memory_schema(connection: sqlite3.Connection) -> None:
    """Compare PRAGMA metadata and parsed CHECKs, not byte-for-byte DDL.

    Includes types, nullability, defaults, PKs, FK columns/actions, unique and
    partial indexes, and every token/content/ownership/state/time invariant.
    The canonical reference is temporary; the supplied connection is read only.
    """
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "(name='assistant_long_term_memories' OR name LIKE 'assistant_memory_%')"
    )}
    if tables != {'assistant_long_term_memories', 'assistant_memory_delete_previews'}:
        raise AssistantMemorySchemaError('Assistant memory tables do not match v15')
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
        "('assistant_long_term_memories','assistant_memory_delete_previews')"
    ).fetchone():
        raise AssistantMemorySchemaError('Unexpected memory trigger')
    reference = sqlite3.connect(':memory:')
    try:
        create_assistant_memory_schema(reference)
        for name in ('assistant_long_term_memories', 'assistant_memory_delete_previews'):
            actual = _table_signature(connection, name)
            expected = _table_signature(reference, name)
            remaining = list(actual[-1])
            for number, invariant in enumerate(expected[-1], 1):
                if invariant not in remaining:
                    raise AssistantMemorySchemaError(
                        f'Missing CHECK invariant #{number} in {name}'
                    )
                remaining.remove(invariant)
            if actual != expected:
                raise AssistantMemorySchemaError("Assistant memory schema invariants do not match v15")
    finally:
        reference.close()
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise AssistantMemorySchemaError("Foreign key check failed for v15")
