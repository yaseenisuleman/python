"""Date and time utilities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

DateInput = Union[str, datetime]


class Dates:
    """Date and time helpers for Kestra tasks."""

    @staticmethod
    def utc_now() -> datetime:
        """Return the current UTC datetime."""
        return datetime.now(timezone.utc)

    @staticmethod
    def parse_datetime(value: DateInput, *, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
        """Parse a datetime string or pass through an existing datetime."""
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        parsed = datetime.strptime(value, fmt)
        return parsed.replace(tzinfo=timezone.utc)
