"""Clock-injectable shift-reminder calculation and Telegram orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.keyboards.warehouse import reminder_notification_keyboard
from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import Clock, SystemClock, utc_rfc3339
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase

logger = logging.getLogger(__name__)


class WarehouseReminderService:
    """Create, claim and deliver reminders without holding a SQLite lock over I/O."""

    def __init__(self, repository: WarehouseReminderDatabase, sender: Any, clock: Clock | None = None) -> None:
        self.repository = repository
        self.sender = sender
        self.clock = clock or SystemClock()

    @staticmethod
    def defaults(source_location_id: int) -> dict[str, Any]:
        return {"source_location_id": source_location_id, "enabled": 0, "target_chat_id": None, "target_title": None, "timezone": "Europe/Kyiv", "first_reminder_minutes_before_close": 30, "repeat_interval_minutes": 30, "max_reminders": 3, "revision": 0}

    @staticmethod
    def schedules(shift: dict[str, Any], settings: dict[str, Any]) -> list[tuple[int, str]]:
        """Calculate only from the UTC close snapshot captured for this shift."""
        close = datetime.fromisoformat(str(shift["scheduled_close_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
        first = int(settings["first_reminder_minutes_before_close"])
        repeat = int(settings["repeat_interval_minutes"])
        maximum = int(settings["max_reminders"])
        return [(sequence, utc_rfc3339(close - timedelta(minutes=first) if sequence == 1 else close + timedelta(minutes=repeat * (sequence - 2)))) for sequence in range(1, maximum + 1)]

    async def run_once(self, now_utc: datetime | None = None) -> None:
        """Process due events once; the optional clock makes this deterministic in tests."""
        now = now_utc or self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        stamp = utc_rfc3339(now)
        for shift in await asyncio.to_thread(self.repository.open_shifts):
            await asyncio.to_thread(self.repository.ensure_events, int(shift["id"]), self.schedules(shift, shift), stamp)
        await asyncio.to_thread(self.repository.cancel_ineligible, stamp)
        due_rows = await asyncio.to_thread(self.repository.due, stamp, utc_rfc3339(now - timedelta(minutes=15)))
        for row in due_rows:
            # claim() checks status, enabled flag and current target in one immediate tx.
            claim = await asyncio.to_thread(self.repository.claim, int(row["id"]), stamp)
            if claim is None:
                continue
            text = f"⏰ Смена за {claim['business_date']} ещё не закрыта.\n\nПроверьте записи и обязательные списания."
            recipients = await asyncio.to_thread(
                self.repository.active_recipients, int(claim["shift_id"])
            )
            failed = False
            last_message_id = await asyncio.to_thread(
                self.repository.successful_delivery_message, int(claim["id"])
            )
            for recipient in recipients:
                owned = await asyncio.to_thread(
                    self.repository.claim_delivery, int(claim["id"]), recipient, stamp
                )
                if not owned:
                    continue
                try:
                    last_message_id = int(await self.sender.send_reminder(
                        chat_id=recipient, text=text,
                        reply_markup=reminder_notification_keyboard()))
                except Exception as exc:
                    failed = True
                    await asyncio.to_thread(self.repository.finish_delivery,
                        int(claim["id"]),recipient,stamp,error_code=type(exc).__name__.casefold())
                else:
                    await asyncio.to_thread(self.repository.finish_delivery,
                        int(claim["id"]),recipient,stamp,message_id=last_message_id)
            # The configured chat is an escalation/fallback, not a parallel recipient.
            if (not recipients or failed) and claim.get("target_chat_id") is not None:
                try:
                    last_message_id = int(await self.sender.send_reminder(
                        chat_id=int(claim["target_chat_id"]), text=text,
                        reply_markup=reminder_notification_keyboard()))
                    failed = False
                except Exception as exc:
                    failed = True
                    error_type = type(exc).__name__.casefold()
            if failed or last_message_id is None:
                await asyncio.to_thread(self.repository.finish, int(claim["id"]), None,
                    stamp, (error_type if 'error_type' in locals() else "no_successful_delivery",
                            "Telegram не принял напоминание"))
            else:
                await asyncio.to_thread(self.repository.finish, int(claim["id"]),
                                        last_message_id, stamp)

    async def run_forever(self, interval: int = 45) -> None:
        """Run until cancelled by the application shutdown path."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Warehouse reminder tick failed")
            await asyncio.sleep(interval)


class WarehouseReminderSettingsService:
    """Authorized, optimistic-concurrency access to one location's settings."""

    def __init__(self, repository: WarehouseReminderDatabase, access: AccessService, clock: Clock | None = None) -> None:
        self.repository = repository
        self.access = access
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder clock must return a timezone-aware datetime")
        return utc_rfc3339(now.astimezone(UTC))

    @staticmethod
    def _write_values(settings: dict[str, Any]) -> dict[str, Any]:
        return {"enabled": int(settings["enabled"]), "target_chat_id": settings["target_chat_id"], "target_title": settings["target_title"], "timezone": settings["timezone"], "first": int(settings["first_reminder_minutes_before_close"]), "repeat": int(settings["repeat_interval_minutes"]), "maximum": int(settings["max_reminders"])}

    @staticmethod
    def _validate(values: dict[str, Any]) -> None:
        try:
            ZoneInfo(str(values["timezone"]))
        except Exception as exc:
            raise ValueError("Нужна корректная IANA timezone") from exc
        for key, lower, upper in (("first", 0, 1440), ("repeat", 5, 1440), ("maximum", 1, 10)):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError("Значение настройки вне допустимого диапазона")
        target_chat_id = values["target_chat_id"]
        if target_chat_id is not None and (isinstance(target_chat_id, bool) or not isinstance(target_chat_id, int) or target_chat_id == 0):
            raise ValueError("Нужен числовой Telegram chat ID")
        title = values["target_title"]
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200):
            raise ValueError("Название рабочего чата должно содержать от 1 до 200 символов")
        if values["enabled"] and (values["target_chat_id"] is None or not str(values["target_title"] or "").strip()):
            raise ValueError("Для включения нужен рабочий чат")

    def get(self, source_location_id: int, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_reminder_management(actor_user_id)
        return self.repository.settings(source_location_id) or WarehouseReminderService.defaults(source_location_id)

    def save(self, source_location_id: int, values: dict[str, Any], expected_revision: int | None, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_reminder_management(actor_user_id)
        self._validate(values)
        return self.repository.save_settings(source_location_id, values, expected_revision, self._now(), actor_user_id=actor_user_id)

    def update(self, source_location_id: int, patch: dict[str, Any], expected_revision: int, actor_user_id: int) -> dict[str, Any]:
        current = self.get(source_location_id, actor_user_id)
        values = self._write_values(current)
        values.update(patch)
        return self.save(source_location_id, values, expected_revision, actor_user_id)
