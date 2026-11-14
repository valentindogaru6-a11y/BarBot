"""Read-only verification projections; hashes only, no row values escape IPC."""
import hashlib
import sqlite3


def business_fingerprint(connection):
    digest = hashlib.sha256()
    tables = [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
        if not r[0].startswith('sqlite_') and r[0] not in {
            'assistant_conversation_sessions', 'assistant_conversation_turns'}]
    for table in tables:
        quoted = '"'+table.replace('"', '""')+'"'
        digest.update(table.encode())
        # Row order is canonicalized by hashed materialized rows, not unspecified
        # SQLite scan order. Neither SQL nor raw memory values are transported.
        rows = sorted(hashlib.sha256(repr(tuple(row)).encode()).digest()
                      for row in connection.execute('SELECT * FROM '+quoted))
        for row in rows:
            digest.update(row)
    return digest.hexdigest()


def integrity_ok(connection):
    return (connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            and not connection.execute('PRAGMA foreign_key_check').fetchall())
