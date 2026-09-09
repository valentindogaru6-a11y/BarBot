"""Kyiv warehouse business-date boundaries with an injectable clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BusinessPeriod:
    business_date: date
    timezone: str
    cutoff: str
    started_at_utc: datetime
    scheduled_close_at_utc: datetime


class BusinessDateService:
    def __init__(
        self,
        *,
        timezone: str = "Europe/Kyiv",
        cutoff: str = "23:00",
        clock: Clock | None = None,
    ) -> None:
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.cutoff = cutoff
        try:
            hour, minute = (int(part) for part in cutoff.split(":"))
            self.cutoff_time = time(hour, minute)
        except (TypeError, ValueError) as exc:
            raise ValueError("cutoff must use HH:MM") from exc
        self.clock = clock or SystemClock()

    def current_period(self) -> BusinessPeriod:
        return self.period_for(self.clock.now())

    def period_for(self, instant: datetime) -> BusinessPeriod:
        if instant.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        local = instant.astimezone(self.zone)
        business_date = local.date()
        if local.timetz().replace(tzinfo=None) >= self.cutoff_time:
            business_date += timedelta(days=1)
        close_local = datetime.combine(
            business_date, self.cutoff_time, tzinfo=self.zone
        )
        start_local = close_local - timedelta(days=1)
        return BusinessPeriod(
            business_date=business_date,
            timezone=self.timezone,
            cutoff=self.cutoff,
            started_at_utc=start_local.astimezone(UTC),
            scheduled_close_at_utc=close_local.astimezone(UTC),
        )


def utc_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
