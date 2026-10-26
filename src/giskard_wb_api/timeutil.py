from datetime import UTC, datetime


def iso(dt: datetime) -> str:
    """ISO 8601 UTC timestamp with milliseconds, e.g. 2026-01-02T03:04:05.678Z."""
    return dt.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def now() -> str:
    return iso(datetime.now(UTC))


def from_epoch(seconds: float) -> str:
    return iso(datetime.fromtimestamp(seconds, UTC))


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
