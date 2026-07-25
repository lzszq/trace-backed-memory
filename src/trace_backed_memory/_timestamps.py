from __future__ import annotations

from datetime import datetime, timezone
import re


RFC3339_PATTERN = (
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)
_RFC3339_RE = re.compile(RFC3339_PATTERN)


def parse_rfc3339(value: object) -> datetime:
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        raise ValueError("value must be a timezone-aware RFC 3339 date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "value must be a timezone-aware RFC 3339 date-time string"
        ) from exc
    if parsed.utcoffset() is None:
        raise ValueError("value must be a timezone-aware RFC 3339 date-time string")
    return parsed


def canonical_rfc3339(value: object) -> str:
    return (
        parse_rfc3339(value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def aware_datetime_to_rfc3339(value: object) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("value must be an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
