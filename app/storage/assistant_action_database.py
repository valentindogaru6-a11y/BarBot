"""Transactional persistence for assistant action proposals."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class AssistantActionConflict(RuntimeError):
    pass


class AssistantActionActiveConflict(AssistantActionConflict):
    """An actor already owns an unfinished proposal."""

    def __init__(self, active: dict[str, Any]) -> None:
        super().__init__("An active proposal already exists")
        self.active = active


class AssistantActionDatabase:
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

    @staticmethod
    def _cleanup(connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            """UPDATE assistant_action_proposals
               SET status='expired',revision=revision+1
               WHERE id IN (
                   SELECT id FROM assistant_action_proposals
                   WHERE status IN ('collecting','ready','confirmed')
                     AND expires_at_utc<=?
                   ORDER BY expires_at_utc,id LIMIT 25
               )""",
            (now,),
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for source, target in (
            ("arguments_json", "arguments"),
            ("preview_json", "preview"),
            ("result_json", "result"),
        ):
            value = result.pop(source)
            result[target] = json.loads(value) if value is not None else None
        return result

    def create(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            active = connection.execute(
                """SELECT * FROM assistant_action_proposals
                   WHERE actor_user_id=?
                     AND status IN ('collecting','ready','confirmed')
                   ORDER BY id DESC LIMIT 1""",
                (values["actor_user_id"],),
            ).fetchone()
            if active is not None:
                active_row = self._row(active)
                assert active_row is not None
                raise AssistantActionActiveConflict(active_row)
            cursor = connection.execute(
                """INSERT INTO assistant_action_proposals (
                    opaque_token,actor_user_id,source_chat_id,source_message_id,
                    capability_id,risk_level,status,target_type,target_id,
                    target_revision,arguments_json,preview_json,reason_text,
                    created_at_utc,expires_at_utc,revision
                ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,1)""",
                (
                    values["opaque_token"], values["actor_user_id"],
                    values["source_chat_id"], values["source_message_id"],
                    values["capability_id"], values["risk_level"],
                    values.get("target_type"), values.get("target_id"),
                    values.get("target_revision"),
                    json.dumps(values["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(values["preview"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    values.get("reason_text"), values["created_at_utc"],
                    values["expires_at_utc"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM assistant_action_proposals WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
            assert row is not None
            result = self._row(row)
            assert result is not None
            return result

    def active_for_actor(self, actor_user_id: int, now: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            return self._row(connection.execute(
                """SELECT * FROM assistant_action_proposals
                   WHERE actor_user_id=?
                     AND status IN ('collecting','ready','confirmed')
                   ORDER BY id DESC LIMIT 1""",
                (actor_user_id,),
            ).fetchone())

    def get_owned(
        self,
        token: str,
        actor_user_id: int,
        source_chat_id: int,
        now: str,
    ) -> dict[str, Any] | None:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            return self._row(connection.execute(
                """SELECT * FROM assistant_action_proposals
                   WHERE opaque_token=? AND actor_user_id=? AND source_chat_id=?""",
                (token, actor_user_id, source_chat_id),
            ).fetchone())

    def transition(
        self,
        token: str,
        actor_user_id: int,
        source_chat_id: int,
        *,
        expected_status: str,
        status: str,
        now: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._cleanup(connection, now)
            timestamps = {
                "confirmed": "confirmed_at_utc",
                "executed": "executed_at_utc",
                "cancelled": "cancelled_at_utc",
                "failed": "confirmed_at_utc",
                "conflicted": "confirmed_at_utc",
            }
            timestamp_column = timestamps.get(status)
            assignments = ["status=?", "revision=revision+1"]
            parameters: list[Any] = [status]
            if timestamp_column is not None:
                assignments.append(f"{timestamp_column}=?")
                parameters.append(now)
            if result is not None:
                assignments.append("result_json=?")
                parameters.append(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            parameters.extend((token, actor_user_id, source_chat_id, expected_status))
            cursor = connection.execute(
                f"""UPDATE assistant_action_proposals SET {','.join(assignments)}
                    WHERE opaque_token=? AND actor_user_id=? AND source_chat_id=?
                      AND status=?""",
                parameters,
            )
            if cursor.rowcount != 1:
                raise AssistantActionConflict("Proposal state changed")
            row = connection.execute(
                "SELECT * FROM assistant_action_proposals WHERE opaque_token=?",
                (token,),
            ).fetchone()
            result_row = self._row(row)
            assert result_row is not None
            return result_row

    def confirm_atomic(
        self,
        token: str,
        actor_user_id: int,
        source_chat_id: int,
        *,
        now: str,
        executor: Callable[[dict[str, Any], sqlite3.Connection], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one SQLite mutation and proposal finalization in one UoW."""

        with self._transaction() as connection:
            self._cleanup(connection, now)
            raw = connection.execute(
                """SELECT * FROM assistant_action_proposals
                   WHERE opaque_token=? AND actor_user_id=? AND source_chat_id=?""",
                (token, actor_user_id, source_chat_id),
            ).fetchone()
            current = self._row(raw)
            if current is None:
                raise AssistantActionConflict("Proposal not found")
            if current["status"] != "ready":
                raise AssistantActionConflict("Proposal is not ready")
            cursor = connection.execute(
                """UPDATE assistant_action_proposals
                   SET status='confirmed',confirmed_at_utc=?,revision=revision+1
                   WHERE opaque_token=? AND actor_user_id=? AND source_chat_id=?
                     AND status='ready'""",
                (now, token, actor_user_id, source_chat_id),
            )
            if cursor.rowcount != 1:
                raise AssistantActionConflict("Proposal state changed")
            confirmed_raw = connection.execute(
                "SELECT * FROM assistant_action_proposals WHERE opaque_token=?",
                (token,),
            ).fetchone()
            confirmed = self._row(confirmed_raw)
            assert confirmed is not None
            result = executor(confirmed, connection)
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cursor = connection.execute(
                """UPDATE assistant_action_proposals
                   SET status='executed',executed_at_utc=?,result_json=?,
                       revision=revision+1
                   WHERE opaque_token=? AND actor_user_id=? AND source_chat_id=?
                     AND status='confirmed'""",
                (now, encoded, token, actor_user_id, source_chat_id),
            )
            if cursor.rowcount != 1:
                raise AssistantActionConflict("Proposal finalization failed")
            executed_raw = connection.execute(
                "SELECT * FROM assistant_action_proposals WHERE opaque_token=?",
                (token,),
            ).fetchone()
            executed = self._row(executed_raw)
            assert executed is not None
            return executed, result
