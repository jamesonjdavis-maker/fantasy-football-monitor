"""Normalized, platform-agnostic player representation.

Both the ESPN and Sleeper clients emit players as plain dicts with this exact
shape so the snapshot, diff, and analysis code never has to care which platform
a player came from. Plain dicts (not dataclasses) keep JSON round-tripping and
day-over-day diffing trivial.
"""

from __future__ import annotations

from typing import Any

# Slot buckets we normalize every platform's lineup slot into.
STARTER = "starter"
BENCH = "bench"
IR = "ir"


def normalize_player(
    *,
    player_id: str,
    name: str,
    position: str | None,
    pro_team: str | None,
    slot: str,
    injury_status: str | None,
    proj_points: float | None,
    actual_points: float | None = None,
    avg_points: float | None = None,
    percent_owned: float | None = None,
    on_bye: bool = False,
) -> dict[str, Any]:
    """Build one normalized player dict. Every field is JSON-serializable."""
    return {
        "id": str(player_id),
        "name": name or "Unknown",
        "position": (position or "").upper() or None,
        "pro_team": (pro_team or "").upper() or None,
        "slot": slot,  # STARTER / BENCH / IR
        "injury_status": _normalize_injury(injury_status),
        "proj_points": _round(proj_points),
        "actual_points": _round(actual_points),
        "avg_points": _round(avg_points),
        "percent_owned": _round(percent_owned),
        "on_bye": bool(on_bye),
    }


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


# Map the many spellings each platform uses into a single vocabulary.
_INJURY_MAP = {
    "ACTIVE": "ACTIVE",
    "NORMAL": "ACTIVE",
    "": "ACTIVE",
    "QUESTIONABLE": "QUESTIONABLE",
    "Q": "QUESTIONABLE",
    "DOUBTFUL": "DOUBTFUL",
    "D": "DOUBTFUL",
    "OUT": "OUT",
    "O": "OUT",
    "INJURY_RESERVE": "IR",
    "IR": "IR",
    "SUSPENSION": "SUSPENDED",
    "SUS": "SUSPENDED",
    "PUP": "PUP",
    "DAY_TO_DAY": "QUESTIONABLE",
    "COVID": "OUT",
    "NA": "OUT",
}


def _normalize_injury(status: str | None) -> str:
    if not status:
        return "ACTIVE"
    return _INJURY_MAP.get(status.strip().upper(), status.strip().upper())


def is_starter(player: dict[str, Any]) -> bool:
    return player.get("slot") == STARTER
