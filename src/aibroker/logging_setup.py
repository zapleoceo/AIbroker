"""Process logging setup shared by every entry point (api, agent workers).

The agent workers used to skip it, so the root logger stayed at Python's default WARNING and
every INFO line of the aibroker loggers (run start, gate decisions, exec event key names) was dropped.
"""
from __future__ import annotations

import logging

from aibroker.config import get_settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """Root logging at LOG_LEVEL (env, default INFO); an unknown level name falls back to INFO."""
    level = getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT)
