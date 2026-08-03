"""Append-only JSONL audit log for administrative operations."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class AuditService:
    """Write sanitized administrative audit records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        admin_user_id: int,
        username: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        parent_ids: dict[str, str],
        description: str,
        status: Literal["success", "error"],
    ) -> bool:
        """Append one audit event; log and suppress filesystem failures."""

        record = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "admin_user_id": admin_user_id,
            "username": username,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "parent_ids": parent_ids,
            "description": description,
            "status": status,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                audit_file.flush()
            return True
        except OSError:
            logger.exception("Could not append administrative audit record")
            return False

    def record_user_event(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        previous_role: str,
        new_role: str,
        action: str,
        status: Literal["success", "error"],
        description: str,
    ) -> bool:
        """Append a sanitized user/role audit event."""

        record = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "previous_role": previous_role,
            "new_role": new_role,
            "action": action,
            "status": status,
            "description": description,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                audit_file.flush()
            return True
        except OSError:
            logger.exception("Could not append user role audit record")
            return False
