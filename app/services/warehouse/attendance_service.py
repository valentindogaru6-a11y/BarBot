"""Personal attendance sessions linked to shared warehouse shifts."""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from typing import Any

from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import BusinessDateService, Clock, SystemClock, utc_rfc3339
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase


class WarehouseAttendanceService:
    def __init__(self, repository: WarehouseAttendanceDatabase, access: AccessService,
                 clock: Clock | None = None) -> None:
        self.repository = repository
        self.access = access
        self.clock = clock or SystemClock()

    def _now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)

    def status(
        self, user_id: int, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        self.access.require_warehouse_attendance(user_id)
        return self.repository.active_for_user(user_id, connection)

    def open(
        self, user_id: int, connection: sqlite3.Connection | None = None
    ) -> tuple[dict[str, Any], bool]:
        context = self.access.require_warehouse_attendance(user_id)
        settings = self.repository.runtime_settings(connection)
        now = self._now()
        period = BusinessDateService(timezone=str(settings["timezone"]),
                                     cutoff=str(settings["cutoff_local_time"]),
                                     clock=self.clock).period_for(now)
        user = context.user
        display_name = user.display_name if user is not None else "Администратор"
        return self.repository.open_session({
            "user_id": user_id, "actor_user_id": user_id,
            "display_name": display_name, "username": user.username if user else None,
            "source_location_id": int(settings["source_location_id"]),
            "business_date": period.business_date.isoformat(),
            "timezone": period.timezone, "cutoff": period.cutoff,
            "started_at_utc": utc_rfc3339(period.started_at_utc),
            "scheduled_close_at_utc": utc_rfc3339(period.scheduled_close_at_utc),
            "now": utc_rfc3339(now),
        }, connection)

    def current_period(self, user_id: int):
        self.access.require_warehouse_attendance(user_id)
        settings = self.repository.runtime_settings()
        return BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        ).current_period()

    def close(
        self, user_id: int, connection: sqlite3.Connection | None = None
    ) -> tuple[dict[str, Any] | None, bool]:
        self.access.require_warehouse_attendance(user_id)
        return self.repository.close_own(
            user_id, utc_rfc3339(self._now()), connection
        )

    def active_participants(self, shift_id: int, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_view(actor_user_id)
        return self.repository.active_for_shift(shift_id)

    def open_sessions(self, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_attendance_management(actor_user_id)
        return self.repository.open_sessions()

    def force_close(self, session_id: int, actor_user_id: int, reason: str,
                    expected_revision: int,
                    connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        self.access.require_warehouse_attendance_management(actor_user_id)
        return self.repository.force_close(
            session_id, actor_user_id, reason, utc_rfc3339(self._now()),
            expected_revision, connection,
        )

    @staticmethod
    def duration_seconds(session: dict[str, Any], now: datetime | None = None) -> int:
        start = datetime.fromisoformat(str(session["opened_at_utc"]).replace("Z", "+00:00"))
        end_text = session.get("closed_at_utc")
        end = datetime.fromisoformat(str(end_text).replace("Z", "+00:00")) if end_text else (now or datetime.now(UTC))
        seconds = int((end - start).total_seconds())
        if seconds < 0:
            raise ValueError("Attendance close precedes open")
        return seconds
