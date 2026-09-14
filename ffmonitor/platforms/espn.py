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

from ..config import ESPNLeague
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


def _recent_actual_points(player: Any, week: int, n: int = 3) -> list[float]:
    """Pull the last `n` completed weeks' actual fantasy points from an espn-api
    player's per-week `stats` dict. Defensive: espn-api's stats shape varies, so
    anything unexpected just yields an empty list (signal skips that player)."""
    stats = getattr(player, "stats", None)
    if not isinstance(stats, dict):
        return []
    out: list[float] = []
    for w in range(max(1, week - n), week):  # prior completed weeks only
        entry = stats.get(w)
        if entry is None:
            entry = stats.get(str(w))
        if isinstance(entry, dict) and entry.get("points") is not None:
            try:
                out.append(round(float(entry["points"]), 2))
            except (TypeError, ValueError):
                pass
    return out


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


def build_league_snapshot(
    entry: ESPNLeague, espn_s2: str | None, swid: str | None, year: int
) -> dict[str, Any]:
    """Return a normalized snapshot for one ESPN league, or an error dict.

    Cookies are account-level, so the same espn_s2/swid are passed for every
    league; only the league_id/team_id differ per `entry`.
    """
    try:
        from espn_api.football import League
    except ImportError:
        return {"enabled": True, "label": entry.label, "platform": "espn",
                "error": "espn-api not installed."}

    base = {"enabled": True, "label": entry.label, "platform": "espn",
            "league_id": str(entry.league_id)}

    try:
        league = League(
            league_id=entry.league_id,
            year=year,
            espn_s2=espn_s2,
            swid=swid,
        )
    except Exception as exc:  # espn-api raises bare Exceptions on auth failure
        return {**base, "error": f"ESPN login failed: {exc}"}

    week = getattr(league, "current_week", None) or 1

    try:
        team, lineup = _find_my_box_lineup(league, entry.team_id, week)
    except Exception as exc:
        return {**base, "error": f"ESPN box score error: {exc}"}

    roster = []
    for bp in lineup:
        pl = _box_player(bp)
        pl["recent_points"] = _recent_actual_points(bp, week)
        roster.append(pl)

    # Everyone rostered anywhere in the league. ESPN's free_agents() can lag and
    # return a player who's actually owned (e.g. a D/ST just picked up), so we
    # cross-check against real rosters and drop anyone owned. league.teams is
    # already loaded, so this costs no extra API calls.
    try:
        rostered_ids = {
            str(getattr(p, "playerId", "")) for t in league.teams for p in t.roster
        }
    except Exception:
        rostered_ids = set()

    try:
        free_agents = []
        for p in league.free_agents(size=60):
            fa = _free_agent(p)
            if fa["id"] in rostered_ids:
                continue
            fa["recent_points"] = _recent_actual_points(p, week)
            free_agents.append(fa)
    except Exception:
        free_agents = []

    return {
        **base,
        "league_name": getattr(league, "settings", None)
        and getattr(league.settings, "name", None),
        "team_name": getattr(team, "team_name", "My Team"),
        "team_id": getattr(team, "team_id", None),
        "week": week,
        "season": str(year),
        "private": bool(espn_s2 and swid),
        "roster": roster,
        "free_agents": free_agents,
    }
