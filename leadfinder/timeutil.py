"""Time helpers. All timestamps are stored in the database as UTC ISO strings
('YYYY-MM-DD HH:MM:SS'). Display and "today" use Europe/London.

Tests can freeze the clock with `set_now()`.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

_frozen: datetime | None = None


def set_now(dt: datetime | None) -> None:
    """Freeze the clock (tests). Pass None to unfreeze."""
    global _frozen
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    _frozen = dt


def now_utc() -> datetime:
    if _frozen is not None:
        return _frozen.astimezone(UTC)
    return datetime.now(UTC).replace(microsecond=0)


def now_london() -> datetime:
    return now_utc().astimezone(LONDON)


def today_london() -> date:
    return now_london().date()


def to_db(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def from_db(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("T", " ").replace("Z", "")
    if "+" in s[10:]:
        s = s[: 10 + s[10:].index("+")]
    fmt = "%Y-%m-%d %H:%M:%S" if len(s) > 10 else "%Y-%m-%d"
    return datetime.strptime(s[:19], fmt).replace(tzinfo=UTC)


def now_db() -> str:
    return to_db(now_utc())  # type: ignore[return-value]


def london_day_bounds(d: date) -> tuple[str, str]:
    """UTC DB strings for the start (inclusive) and end (exclusive) of a London calendar day."""
    start = datetime.combine(d, time(0, 0), tzinfo=LONDON)
    end = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=LONDON)
    return to_db(start), to_db(end)  # type: ignore[return-value]


def london_local(dt_utc: datetime | str | None) -> datetime | None:
    if isinstance(dt_utc, str):
        dt_utc = from_db(dt_utc)
    if dt_utc is None:
        return None
    return dt_utc.astimezone(LONDON)


def parse_local(value: str) -> datetime:
    """Parse a user-entered London local date or datetime ('2026-10-20' or '2026-10-20T14:00')
    and return an aware UTC datetime. A bare date means 10:00 that day."""
    value = value.strip().replace(" ", "T")
    if "T" in value:
        naive = datetime.fromisoformat(value)
    else:
        naive = datetime.combine(date.fromisoformat(value), time(10, 0))
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=LONDON)
    return naive.astimezone(UTC)


def fmt_local(dt: datetime | str | None, with_time: bool = True) -> str:
    loc = london_local(dt)
    if loc is None:
        return ""
    return loc.strftime("%a %d %b %Y %H:%M" if with_time else "%a %d %b %Y")


def pacific_month_window(at: datetime | None = None) -> tuple[datetime, datetime]:
    """Start and end (UTC) of the current Google billing month, which runs on Pacific time."""
    at = (at or now_utc()).astimezone(PACIFIC)
    start = datetime(at.year, at.month, 1, tzinfo=PACIFIC)
    if at.month == 12:
        end = datetime(at.year + 1, 1, 1, tzinfo=PACIFIC)
    else:
        end = datetime(at.year, at.month + 1, 1, tzinfo=PACIFIC)
    return start.astimezone(UTC), end.astimezone(UTC)


def humanize_ago(dt: datetime | str | None) -> str:
    if isinstance(dt, str):
        dt = from_db(dt)
    if dt is None:
        return "never"
    secs = (now_utc() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
