"""Input validation helpers."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


class Validation:
    """Standalone validation helpers for Kestra tasks."""

    @staticmethod
    def require_not_none(value: T | None, name: str) -> T:
        """Raise ValueError if value is None."""
        if value is None:
            raise ValueError(f"{name} must not be None")
        return value

    @staticmethod
    def require_non_empty(value: str, name: str) -> str:
        """Raise ValueError if a string is empty or whitespace."""
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")
        return value.strip()
