from datetime import UTC, datetime

import pytest

from app.services.warehouse.business_date_service import BusinessDateService


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


@pytest.mark.parametrize(
    ("local_hour", "expected"),
    [
        ((2026, 9, 8, 22, 59), "2026-09-08"),
        ((2026, 9, 8, 23, 0), "2026-09-09"),
        ((2026, 9, 8, 23, 1), "2026-09-09"),
        ((2026, 9, 9, 1, 0), "2026-09-09"),
    ],
)
def test_kyiv_cutoff(local_hour, expected) -> None:
    from zoneinfo import ZoneInfo

    value = datetime(*local_hour, tzinfo=ZoneInfo("Europe/Kyiv"))
    assert BusinessDateService().period_for(value).business_date.isoformat() == expected


def test_period_uses_utc_and_injected_clock() -> None:
    value = datetime(2026, 1, 8, 21, 0, tzinfo=UTC)
    period = BusinessDateService(clock=FrozenClock(value)).current_period()
    assert period.business_date.isoformat() == "2026-01-09"
    assert period.started_at_utc == datetime(2026, 1, 8, 21, 0, tzinfo=UTC)
    assert period.scheduled_close_at_utc == datetime(2026, 1, 9, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "close_hour"),
    [
        (datetime(2026, 3, 28, 20, 0, tzinfo=UTC), 21),
        (datetime(2026, 7, 8, 20, 0, tzinfo=UTC), 20),
    ],
)
def test_kyiv_dst_conversion(value, close_hour) -> None:
    period = BusinessDateService().period_for(value)
    assert period.scheduled_close_at_utc.hour == close_hour


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        BusinessDateService().period_for(datetime(2026, 1, 1))
