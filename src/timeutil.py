from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local_now(tz_name: str) -> datetime:
    return datetime.now(zone(tz_name))


def local_today(tz_name: str) -> date:
    return local_now(tz_name).date()


def parse_hhmm(s: str) -> time:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Bad time: {s}")
    return time(int(parts[0]), int(parts[1]))


def is_weekday(tz_name: str) -> bool:
    return local_now(tz_name).weekday() < 5


# Тестовые переопределения (только для ручной проверки; сброс: /test_reset)
_test_weekday_override: bool | None = None
_test_deadline_override: bool | None = None


def set_test_weekday_override(value: bool | None) -> None:
    """None — календарь как обычно; True — как будний; False — как выходной."""
    global _test_weekday_override
    _test_weekday_override = value


def set_test_deadline_override(value: bool | None) -> None:
    """None — смотреть на часы; True — считать дедлайн прошедшим; False — не прошедшим."""
    global _test_deadline_override
    _test_deadline_override = value


def is_weekday_effective(tz_name: str) -> bool:
    if _test_weekday_override is not None:
        return _test_weekday_override
    return is_weekday(tz_name)


def is_deadline_passed(tz_name: str, deadline_hhmm: str) -> bool:
    if _test_deadline_override is not None:
        return _test_deadline_override
    now = local_now(tz_name)
    t = parse_hhmm(deadline_hhmm)
    dl = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return now > dl


def previous_month(d: date) -> tuple[int, int]:
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1
