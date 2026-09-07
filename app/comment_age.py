from datetime import datetime, timedelta, timezone

from app import config


def parse_platform_timestamp(value: str | int | float | None) -> datetime | None:
    """Parse ISO timestamps and Meta epoch seconds as timezone-aware UTC values."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, ValueError):
            return None
    normalized = str(value).strip().replace("Z", "+00:00")
    if normalized.endswith("+0000"):
        normalized = normalized[:-5] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_within_comment_age_limit(
    published_at: str | int | float | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return false only when a valid timestamp is older than the configured limit."""
    published = parse_platform_timestamp(published_at)
    if published is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(
        days=max(1, config.COMMENT_MAX_AGE_DAYS)
    )
    return published >= cutoff
