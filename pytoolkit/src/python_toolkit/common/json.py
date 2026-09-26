"""JSON parsing and validation helpers.

Kestra usage::

    from python_toolkit.common.json import JsonTools
"""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence


class JsonValidationError(ValueError):
    """Raised when JSON input fails validation."""


class JsonTools:
    """JSON helpers for Kestra tasks."""

    @staticmethod
    def validate_object(
        value: str,
        *,
        name: str = "config",
        required_keys: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Parse a JSON string, ensure it is an object, and check required keys."""
        if not value or not value.strip():
            raise JsonValidationError(f"{name} is empty")

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JsonValidationError(
                f"{name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ) from None

        if not isinstance(parsed, dict):
            raise JsonValidationError(f"{name} JSON is valid but not an object")

        for key in required_keys or ():
            if key not in parsed:
                raise JsonValidationError(f"{name} JSON is valid but missing '{key}'")

        return parsed

    @staticmethod
    def validate_object_or_fail(
        value: str,
        *,
        name: str = "config",
        required_keys: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Validate JSON or exit with a one-line error — no traceback."""
        try:
            return JsonTools.validate_object(
                value,
                name=name,
                required_keys=required_keys,
            )
        except JsonValidationError as exc:
            sys.exit(str(exc))
