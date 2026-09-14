"""Assemble a full daily snapshot across all enabled platforms."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import __version__
from .config import Config
from .platforms import espn, sleeper


def build(config: Config) -> dict[str, Any]:
    """Collect snapshots from every enabled platform into one dict."""
    platforms: dict[str, Any] = {}

    if config.espn.enabled:
        platforms["espn"] = espn.build_snapshot(config.espn)
    if config.sleeper.enabled:
        platforms["sleeper"] = sleeper.build_snapshot(config.sleeper, config.data_dir)

    return {
        "schema_version": 1,
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
    }
