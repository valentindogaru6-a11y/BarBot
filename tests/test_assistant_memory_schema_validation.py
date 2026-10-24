"""Real malformed SQLite schemas must fail the production v15 validator."""

import re
import sqlite3

import pytest

from app.storage.assistant_memory_schema import (
    SCHEMA_STATEMENTS, AssistantMemorySchemaError, validate_assistant_memory_schema,
)


def cases():
    result = []
    for statement_index in (0, 3):
        sql = SCHEMA_STATEMENTS[statement_index]
        # Remove each individual CHECK, one invariant at a time, using balanced
        # parentheses. Table-level clauses include their preceding comma.
        for index, match in enumerate(re.finditer(r'\bCHECK\s*\(', sql)):
            depth, end, quote = 1, match.end(), False
            while depth:
                char = sql[end]
                if char == "'":
                    quote = not quote
                elif not quote:
                    depth += (char == '(') - (char == ')')
                end += 1
            start = match.start()
            prefix = sql[:start].rstrip()
            if prefix.endswith(','):
                start = len(prefix) - 1
            weakened = sql[:start] + sql[end:]
            # CHECK may be the only item on a line and followed by another
            # table constraint. The preceding comma removal is deliberate.
            result.append((f'{statement_index}-check-{index}', statement_index, weakened))
        for index, match in enumerate(re.finditer(r'^\s+\w+ (?:TEXT|INTEGER) (NOT NULL)', sql, re.M)):
            result.append((f'{statement_index}-nullability-{index}', statement_index,
                sql[:match.start(1)] + sql[match.end(1):]))
        for column in re.findall(r'^\s+(\w+) (?:INTEGER|TEXT)', sql, re.M):
            result.append((f'{statement_index}-type-{column}', statement_index,
                re.sub(rf'\b{column} (?:INTEGER|TEXT)', f'{column} BLOB', sql, count=1)))
        result.append((f'{statement_index}-primary-key',statement_index,sql.replace('id INTEGER PRIMARY KEY','id INTEGER')))
        result.append((f'{statement_index}-token-unique',statement_index,sql.replace('NOT NULL UNIQUE','NOT NULL',1)))
    result.extend([
        ('check-in-comment',0,SCHEMA_STATEMENTS[0].replace('CHECK(revision>0)', '/* CHECK(revision>0) */')),
        ('status-literal-case',0,SCHEMA_STATEMENTS[0].replace("'active'", "'ACTIVE'")),
        ('token-character-class',0,SCHEMA_STATEMENTS[0].replace('A-Za-z0-9_-','A-Za-z0-9_?')),
        ('default',0,SCHEMA_STATEMENTS[0].replace('DEFAULT 1','DEFAULT 2')),
        ('owner-composite-unique',0,SCHEMA_STATEMENTS[0].replace('UNIQUE(id,owner_user_id,source_chat_id),','')),
        ('fk-target',0,SCHEMA_STATEMENTS[0].replace('REFERENCES assistant_long_term_memories(id,owner_user_id,source_chat_id)', 'REFERENCES assistant_long_term_memories(id,source_chat_id,owner_user_id)')),
        ('delete-fk-action',3,SCHEMA_STATEMENTS[3].replace('ON DELETE CASCADE','ON DELETE NO ACTION')),
        ('delete-fk-columns',3,SCHEMA_STATEMENTS[3].replace('FOREIGN KEY(memory_id,owner_user_id,source_chat_id)','FOREIGN KEY(memory_id,source_chat_id,owner_user_id)')),
        ('index-column',1,SCHEMA_STATEMENTS[1].replace('source_chat_id,','')),
        ('index-uniqueness',2,SCHEMA_STATEMENTS[2].replace('CREATE UNIQUE INDEX','CREATE INDEX')),
        ('index-predicate',2,SCHEMA_STATEMENTS[2].replace("status='active'","status='pending'")),
    ])
    return result


@pytest.mark.parametrize('label,index,replacement', cases(), ids=[case[0] for case in cases()])
def test_validator_rejects_each_missing_invariant(label,index,replacement):
    del label
    with sqlite3.connect(':memory:') as connection:
        for number, statement in enumerate(SCHEMA_STATEMENTS):
            connection.execute(replacement if number == index else statement)
        with pytest.raises(AssistantMemorySchemaError):
            validate_assistant_memory_schema(connection)


def test_equivalent_formatted_schema_is_accepted():
    with sqlite3.connect(':memory:') as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement.replace('CHECK(', 'CHECK (\n').replace('NOT NULL','not null'))
        validate_assistant_memory_schema(connection)


@pytest.mark.parametrize('index', (0,3))
def test_missing_table_is_rejected(index):
    with sqlite3.connect(':memory:') as connection:
        for number, statement in enumerate(SCHEMA_STATEMENTS):
            if number != index and not (index==0 and number in (1,2)):
                connection.execute(statement)
        with pytest.raises(AssistantMemorySchemaError):
            validate_assistant_memory_schema(connection)


@pytest.mark.parametrize('change',[
    {'owner_user_id':0}, {'source_chat_id':0}, {'opaque_token':'x'},
    {'opaque_token':'bad?tokenabcdefgh'}, {'kind':'administrator'}, {'scope':'global'},
    {'source':'provider'}, {'status':'confirmed'}, {'normalized_content':''},
    {'normalized_content':'x'*2001}, {'revision':0}, {'created_at_utc':'invalid'},
    {'updated_at_utc':'invalid'}, {'updated_at_utc':'2025-01-01T00:00:00Z'},
    {'expires_at_utc':'invalid'}, {'expires_at_utc':'2025-01-01T00:00:00Z'},
    {'confirmation_expires_at_utc':'invalid'},
    {'confirmation_expires_at_utc':'2025-01-01T00:00:00Z'},
    {'status':'pending','expires_at_utc':None},
    {'status':'pending','confirmation_expires_at_utc':None},
])
def test_constraints_reject_invalid_rows(change):
    row=dict(opaque_token='abcdefghijklmnop',owner_user_id=1,source_chat_id=10,
        scope='user',kind='terminology',normalized_content='valid',source='explicit_remember',
        status='pending',created_at_utc='2026-01-01T00:00:00Z',updated_at_utc='2026-01-01T00:00:00Z',
        expires_at_utc='2026-01-01T00:15:00Z',confirmation_expires_at_utc='2026-01-01T00:15:00Z',revision=1)
    row.update(change)
    with sqlite3.connect(':memory:') as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"INSERT INTO assistant_long_term_memories ({','.join(row)}) VALUES({','.join('?' for _ in row)})",tuple(row.values()))


@pytest.mark.parametrize('extra', [
    'CREATE TABLE assistant_memory_unknown(id INTEGER)',
    'CREATE TRIGGER unexpected AFTER INSERT ON assistant_long_term_memories BEGIN SELECT 1; END',
])
def test_unexpected_memory_objects_fail(extra):
    with sqlite3.connect(':memory:') as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(extra)
        with pytest.raises(AssistantMemorySchemaError):
            validate_assistant_memory_schema(connection)
