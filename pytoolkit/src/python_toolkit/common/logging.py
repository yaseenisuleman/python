"""Logging helpers for script and CLI tasks."""

from __future__ import annotations

import logging
import sys
from typing import Optional


class ToolkitLogger:
    """Configure stdout logging for Kestra Python tasks."""

    @staticmethod
    def get_logger(
        name: str,
        *,
        level: int = logging.INFO,
        fmt: Optional[str] = None,
    ) -> logging.Logger:
        """Return a logger configured for stdout output."""
        logger = logging.getLogger(name)

        if logger.handlers:
            return logger

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(fmt or "%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )

        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
        return logger
