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

    # One entry per ESPN league, keyed "espn:<label>" so each league diffs
    # against its own prior day and shows up separately in alerts.
    if config.espn.enabled:
        for entry in config.espn.leagues:
            key = f"espn:{entry.label}"
            platforms[key] = espn.build_league_snapshot(
                entry, config.espn.espn_s2, config.espn.swid, config.espn.year
            )
    if config.sleeper.enabled:
        platforms["sleeper"] = sleeper.build_snapshot(config.sleeper, config.data_dir)

    return {
        "schema_version": 1,
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
    }
