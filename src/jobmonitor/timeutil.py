"""UTC storage, California display."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    dt = to_utc(dt)
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def from_epoch_ms(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def pacific_display(dt: datetime | str | None) -> str:
    if isinstance(dt, str):
        dt = parse_iso(dt)
    if dt is None:
        return ""
    return to_utc(dt).astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z")
