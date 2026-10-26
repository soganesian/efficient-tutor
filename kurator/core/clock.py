"""Time source. Everything in UTC (SPEC §1); local time only when rendering."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Test clock: time only moves when the test moves it."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment.astimezone(UTC)
        return self._now


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def to_local(moment: datetime, tz: str) -> datetime:
    return moment.astimezone(ZoneInfo(tz))


def local_to_utc(naive_local: datetime, tz: str) -> datetime:
    return naive_local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


# Date and time the tutor writes as text: "16.09 17:00", "16.09.2026 17.00".
_HUMAN_DATETIME = re.compile(
    r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?[\s,]+(\d{1,2})[:.](\d{2})\s*$"
)


def parse_local_datetime(value: str, tz: str, now: datetime) -> datetime | None:
    """Parse "16.09 17:00" in the person's timezone and return UTC. None means parsing failed.

    The year is optional: if omitted, use the nearest year in which the date is still in the future,
    otherwise a December-to-January rollover would shift it back by a year.
    """
    match = _HUMAN_DATETIME.match(value or "")
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    local_now = to_local(now, tz)
    year_num = int(year) if year else local_now.year
    if year and year_num < 100:
        year_num += 2000
    try:
        local = datetime(year_num, int(month), int(day), int(hour), int(minute))
    except ValueError:
        return None
    if not year and local.replace(tzinfo=ZoneInfo(tz)) < local_now:
        try:
            local = local.replace(year=year_num + 1)
        except ValueError:  # February 29
            return None
    return local_to_utc(local, tz)
