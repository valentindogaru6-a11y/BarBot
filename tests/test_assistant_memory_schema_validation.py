"""Real malformed SQLite schemas must fail the production v15 validator."""

import re
import sqlite3

import pytest

from app.storage.assistant_memory_schema import (
    SCHEMA_STATEMENTS, AssistantMemorySchemaError, _checks,
    validate_assistant_memory_schema,
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


SPOOF_INVARIANTS = (
    ('revision', 'revision>0'),
    ('status', "status IN ('pending','active','superseded','deleted','cancelled')"),
    ('kind', "kind IN (\n            'terminology','product_alias','user_preference',\n            'workflow_preference','standing_instruction','business_fact'\n        )"),
    ('token', "\n            length(opaque_token) BETWEEN 16 AND 32\n            AND opaque_token NOT GLOB '*[^A-Za-z0-9_-]*'\n        "),
    ('content', 'length(normalized_content) BETWEEN 1 AND 2000'),
    ('expiry', 'expires_at_utc IS NULL OR julianday(expires_at_utc)>julianday(created_at_utc)'),
    ('timestamp', 'julianday(updated_at_utc)>=julianday(created_at_utc)'),
)


def _spoofed_schema(expression, style):
    clause = f'CHECK({expression})'
    sql = SCHEMA_STATEMENTS[0]
    assert clause in sql
    # Table CHECKs have their own leading comma; column CHECKs do not.
    if ',\n        ' + clause in sql:
        sql = sql.replace(',\n        ' + clause, '', 1)
    else:
        sql = sql.replace(clause, '', 1)
    fake = clause.replace('\n', ' ')
    # SQLite cannot escape ']' inside bracket identifiers. Keep that spoof
    # syntactically valid when the token GLOB expression itself contains ']'.
    bracket_fake = fake.replace(']', '')
    decorations = {
        'absent': '',
        'double': 'CONSTRAINT "' + fake.replace('"', '""') + '" ',
        'single': "CONSTRAINT '" + fake.replace("'", "''") + "' ",
        'backtick': 'CONSTRAINT `' + fake.replace('`', '``') + '` ',
        'bracket': 'CONSTRAINT [' + bracket_fake + '] ',
        'line-comment': '-- ' + fake + '\n ',
        'block-comment': '/* ' + fake + ' */ ',
        'partial-name': 'CONSTRAINT "FAKE' + fake.replace('"', '""') + '" ',
    }
    return sql.replace('FOREIGN KEY(', decorations[style] + 'FOREIGN KEY(', 1)


@pytest.mark.parametrize('label,expression', SPOOF_INVARIANTS,
                         ids=[item[0] for item in SPOOF_INVARIANTS])
@pytest.mark.parametrize('style', (
    'absent', 'double', 'single', 'backtick', 'bracket',
    'line-comment', 'block-comment', 'partial-name',
))
def test_real_schema_rejects_quoted_or_commented_check_spoofs(label, expression, style):
    del label
    replacement = _spoofed_schema(expression, style)
    # The fake marker must not compensate for any missing expression, even
    # before PRAGMA metadata comparison can independently reject the schema.
    assert len(_checks(replacement)) == len(_checks(SCHEMA_STATEMENTS[0])) - 1
    with sqlite3.connect(':memory:') as connection:
        for index, statement in enumerate(SCHEMA_STATEMENTS):
            connection.execute(replacement if index == 0 else statement)
        with pytest.raises(AssistantMemorySchemaError, match='Missing CHECK invariant #'):
            validate_assistant_memory_schema(connection)


@pytest.mark.parametrize('clause', (
    'CHECK(revision > 0)',
    'cHeCk /* comment */ (\n REVISION\n > 0\n)',
    'CHECK((((revision > 0))))',
    'CHECK("revision" > 0)',
    'CHECK(`revision` > 0)',
    'CHECK([revision] > 0)',
))
def test_real_revision_check_accepts_equivalent_token_formatting(clause):
    with sqlite3.connect(':memory:') as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement.replace('CHECK(revision>0)', clause))
        validate_assistant_memory_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('''INSERT INTO assistant_long_term_memories
                (opaque_token,owner_user_id,source_chat_id,scope,kind,normalized_content,
                 source,status,created_at_utc,updated_at_utc,revision)
                VALUES ('abcdefghijklmnop',1,1,'user','terminology','valid',
                        'explicit_remember','active','2026-01-01','2026-01-01',0)''')


@pytest.mark.parametrize('sql', (
    "DEFAULT 'CHECK(revision>0)'",
    "DEFAULT 'it''s CHECK(revision>0)'",
    'CONSTRAINT "escaped""CHECK(revision>0)"',
    'CONSTRAINT `escaped``CHECK(revision>0)`',
    'CONSTRAINT [CHECK(revision>0)]',
    '-- CHECK(revision>0)\n',
    '/* CHECK(revision>0) */',
    'FAKECHECK(revision>0)',
    '$CHECK(revision>0)',
    '1CHECK(revision>0)',
    'CHECK_suffix(revision>0)',
))
def test_lexer_does_not_extract_quoted_commented_or_partial_keyword(sql):
    assert _checks(sql) == ()


def test_multiple_real_checks_and_inert_quoted_content():
    sql = '''CREATE TABLE t (
        revision INTEGER CHECK(revision>0),
        marker TEXT CHECK(marker != 'it''s CHECK(fake>0) /* -- */'),
        "CHECK(fake>0)" INTEGER CHECK("CHECK(fake>0)" >= 0),
        `escaped``CHECK(fake>0)` INTEGER,
        [CHECK(fake>0)] TEXT,
        CHECK(length(marker) > (1 + (2)))
    )'''
    assert len(_checks(sql)) == 4
    assert _checks("CHECK(x != '--')") != _checks("CHECK(x != '/* */')")
    assert _checks("CHECK(x != 'ACTIVE')") != _checks("CHECK(x != 'active')")
    assert _checks("CHECK(x != 'a b')") != _checks("CHECK(x != 'ab')")
    with sqlite3.connect(':memory:') as connection:
        # Duplicate names are irrelevant to lexer coverage; execute a minimal
        # valid form to prove quote/comment-like literal text is legal SQLite.
        connection.execute("CREATE TABLE t(x TEXT CHECK(x != 'it''s CHECK(fake>0) /* -- */'))")


@pytest.mark.parametrize('sql', ("CHECK(x>0", "CHECK()", "CHECK('x)",
                               'CHECK("x)', 'CHECK(`x)', 'CHECK([x)', '/* open'))
def test_lexer_fails_closed_on_malformed_ddl(sql):
    with pytest.raises(AssistantMemorySchemaError):
        _checks(sql)


def test_original_constraint_name_spoof_is_rejected_despite_accepting_bad_row():
    with sqlite3.connect(':memory:') as connection:
        for index, statement in enumerate(SCHEMA_STATEMENTS):
            connection.execute(_spoofed_schema('revision>0', 'double') if index == 0 else statement)
        with pytest.raises(AssistantMemorySchemaError, match='Missing CHECK invariant #'):
            validate_assistant_memory_schema(connection)
        connection.execute('''INSERT INTO assistant_long_term_memories
            (opaque_token,owner_user_id,source_chat_id,scope,kind,normalized_content,
             source,status,created_at_utc,updated_at_utc,revision)
            VALUES ('abcdefghijklmnop',1,1,'user','terminology','valid',
                    'explicit_remember','active','2026-01-01','2026-01-01',0)''')
        assert connection.execute('SELECT revision FROM assistant_long_term_memories').fetchone()[0] == 0
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_string_default_cannot_supply_missing_revision_check():
    sql = SCHEMA_STATEMENTS[0].replace(
        'DEFAULT 1 CHECK(revision>0)', "DEFAULT 'CHECK(revision>0)'"
    )
    assert len(_checks(sql)) == len(_checks(SCHEMA_STATEMENTS[0])) - 1
    with sqlite3.connect(':memory:') as connection:
        for index, statement in enumerate(SCHEMA_STATEMENTS):
            connection.execute(sql if index == 0 else statement)
        with pytest.raises(AssistantMemorySchemaError, match='Missing CHECK invariant #'):
            validate_assistant_memory_schema(connection)


def test_unicode_lookalike_identifier_is_not_the_revision_column():
    sql = SCHEMA_STATEMENTS[0].replace('CHECK(revision>0)', 'CHECK("reviſion">0)')
    with sqlite3.connect(':memory:') as connection:
        for index, statement in enumerate(SCHEMA_STATEMENTS):
            connection.execute(sql if index == 0 else statement)
        with pytest.raises(AssistantMemorySchemaError, match='Missing CHECK invariant #'):
            validate_assistant_memory_schema(connection)
        # Under SQLite DQS compatibility this is an unrelated constant string;
        # Unicode casefold must not normalize it to the real column identifier.
        assert connection.execute('SELECT "reviſion">0').fetchone()[0] == 1


def test_validation_is_read_only_and_does_not_probe_inserts():
    with sqlite3.connect(':memory:') as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        statements = []
        connection.set_trace_callback(statements.append)
        connection.execute('PRAGMA query_only=ON')
        validate_assistant_memory_schema(connection)
        assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE'))
                       for sql in statements)
