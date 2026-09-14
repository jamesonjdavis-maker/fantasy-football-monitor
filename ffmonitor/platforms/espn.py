"""ESPN platform client (wraps the `espn-api` package).

Public leagues need only ESPN_LEAGUE_ID. Private leagues also need the
`espn_s2` and `SWID` cookies from a logged-in browser session, supplied via
GitHub Secrets — never hardcoded.

We use `league.box_scores(week)` to get each starter/bench player's *weekly*
projected and actual points (season averages aren't enough to spot a bench
player beating a starter this week, or to detect a bye — a bye shows up as a
0-point weekly projection for an otherwise-active player).
"""

from __future__ import annotations

from typing import Any

from ..config import ESPNConfig
from ..models import BENCH, IR, STARTER, normalize_player

# espn-api lineupSlot / slot_position strings that are not active starters.
_BENCH_SLOTS = {"BE", "Bench"}
_IR_SLOTS = {"IR"}


def _slot_bucket(slot_position: str | None) -> str:
    if slot_position in _IR_SLOTS:
        return IR
    if slot_position in _BENCH_SLOTS:
        return BENCH
    return STARTER


def _box_player(bp: Any) -> dict[str, Any]:
    """Convert an espn-api BoxPlayer into a normalized player dict."""
    proj = getattr(bp, "projected_points", None)
    actual = getattr(bp, "points", None)
    slot = _slot_bucket(getattr(bp, "slot_position", None))
    # A player whose team is on bye projects 0 for the week while still being
    # rostered/active; that's our bye signal.
    on_bye = slot == STARTER and (proj == 0) and not getattr(bp, "injuryStatus", "")
    return normalize_player(
        player_id=getattr(bp, "playerId", getattr(bp, "name", "?")),
        name=getattr(bp, "name", "Unknown"),
        position=getattr(bp, "position", None),
        pro_team=getattr(bp, "proTeam", None),
        slot=slot,
        injury_status=getattr(bp, "injuryStatus", None),
        proj_points=proj,
        actual_points=actual,
        avg_points=getattr(bp, "avg_points", None),
        percent_owned=getattr(bp, "percent_owned", None),
        on_bye=bool(on_bye),
    )


def _free_agent(p: Any) -> dict[str, Any]:
    return normalize_player(
        player_id=getattr(p, "playerId", getattr(p, "name", "?")),
        name=getattr(p, "name", "Unknown"),
        position=getattr(p, "position", None),
        pro_team=getattr(p, "proTeam", None),
        slot="free_agent",
        injury_status=getattr(p, "injuryStatus", None),
        proj_points=getattr(p, "projected_avg_points", None),
        avg_points=getattr(p, "avg_points", None),
        percent_owned=getattr(p, "percent_owned", None),
    )


def _find_my_box_lineup(league: Any, team_id: int | None, week: int) -> tuple[Any, list]:
    """Return (team, lineup) for my team from this week's box scores."""
    box_scores = league.box_scores(week)
    for box in box_scores:
        for team, lineup in (
            (box.home_team, box.home_lineup),
            (box.away_team, box.away_lineup),
        ):
            if team is None:
                continue
            if team_id is not None and getattr(team, "team_id", None) == team_id:
                return team, lineup
    # No team_id match (or none configured): fall back to the first team.
    first = box_scores[0]
    return first.home_team, first.home_lineup


def build_snapshot(cfg: ESPNConfig) -> dict[str, Any]:
    """Return a normalized snapshot for the ESPN league, or an error dict."""
    if not cfg.enabled:
        return {"enabled": False}

    try:
        from espn_api.football import League
    except ImportError:
        return {"enabled": True, "error": "espn-api not installed."}

    try:
        league = League(
            league_id=cfg.league_id,
            year=cfg.year,
            espn_s2=cfg.espn_s2,
            swid=cfg.swid,
        )
    except Exception as exc:  # espn-api raises bare Exceptions on auth failure
        return {"enabled": True, "error": f"ESPN login failed: {exc}"}

    week = getattr(league, "current_week", None) or 1

    try:
        team, lineup = _find_my_box_lineup(league, cfg.team_id, week)
    except Exception as exc:
        return {"enabled": True, "error": f"ESPN box score error: {exc}"}

    roster = [_box_player(bp) for bp in lineup]

    try:
        free_agents = [_free_agent(p) for p in league.free_agents(size=50)]
    except Exception:
        free_agents = []

    return {
        "enabled": True,
        "league_id": str(cfg.league_id),
        "league_name": getattr(league, "settings", None)
        and getattr(league.settings, "name", None),
        "team_name": getattr(team, "team_name", "My Team"),
        "team_id": getattr(team, "team_id", None),
        "week": week,
        "season": str(cfg.year),
        "private": cfg.is_private,
        "roster": roster,
        "free_agents": free_agents,
    }
