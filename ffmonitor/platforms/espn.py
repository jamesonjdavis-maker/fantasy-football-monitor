"""ESPN platform client (wraps the `espn-api` package).

Public leagues need only ESPN_LEAGUE_ID. Private leagues also need the
`espn_s2` and `SWID` cookies from a logged-in browser session, supplied via
GitHub Secrets — never hardcoded.

We use `league.box_scores(week)` to get each starter/bench player's *weekly*
projected and actual points (season averages aren't enough to spot a bench
player beating a starter this week, or to detect a bye — a bye shows up as a
0-point weekly projection for an otherwise-active player).

Rest-of-season weekly projections aren't exposed by ESPN's API (only the current
week and a season average), so we compute a matchup-adjusted line ourselves via
`ffmonitor.ml.matchup`.
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


# NFL regular season runs through week 18; project the remaining weeks up to it.
_SEASON_END_WEEK = 18


def _ros_projections(player: Any, week: int) -> dict[str, float] | None:
    """Rest-of-season weekly projections {week: points} from the player's per-week
    `stats` dict, for the current week through the end of the regular season.
    ESPN publishes a projected_points entry per scoring period; weeks it hasn't
    projected yet are simply left out. Returns None if nothing is available."""
    stats = getattr(player, "stats", None)
    if not isinstance(stats, dict):
        return None
    out: dict[str, float] = {}
    for w in range(week, _SEASON_END_WEEK + 1):
        entry = stats.get(w)
        if entry is None:
            entry = stats.get(str(w))
        if isinstance(entry, dict) and entry.get("projected_points") is not None:
            try:
                out[str(w)] = round(float(entry["projected_points"]), 2)
            except (TypeError, ValueError):
                pass
    return out or None


def _player_ros(obj: Any, week: int, weekly: dict[str, dict[str, float]] | None) -> dict[str, float] | None:
    """Fallback rest-of-season projections straight from the player's stats dict
    (usually just the current week). The matchup model overwrites this when its
    data is available; this keeps at least one week if it isn't."""
    return _ros_projections(obj, week)


def _apply_ros_projections(player_groups: list[list[dict]], week: int, year: int) -> None:
    """Overwrite each player's ros_proj with a matchup-adjusted weekly line
    (per-game baseline scaled by each week's opponent defense, byes = 0).
    Best-effort: if nflverse data can't load, players keep their fallback."""
    try:
        from ..ml import matchup
    except Exception:
        return
    through = max(1, week - 1)  # defense stats through completed weeks only
    for group in player_groups:
        for p in group:
            base = p.get("avg_points") or p.get("proj_points")
            ros = matchup.build_ros(
                base, p.get("position"), p.get("pro_team"),
                week, _SEASON_END_WEEK, year, through,
            )
            if ros:
                p["ros_proj"] = ros


def _box_player(bp: Any, week: int, weekly: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
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
        lineup_slot=getattr(bp, "slot_position", None),
        injury_status=getattr(bp, "injuryStatus", None),
        proj_points=proj,
        actual_points=actual,
        avg_points=getattr(bp, "avg_points", None),
        percent_owned=getattr(bp, "percent_owned", None),
        on_bye=bool(on_bye),
        ros_proj=_player_ros(bp, week, weekly),
    )


def _free_agent(p: Any, week: int, weekly: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
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
        ros_proj=_player_ros(p, week, weekly),
    )


def _settings_slot(slot_position: str | None) -> str | None:
    """Map an ESPN starting-slot to a replacement bucket (None = not counted:
    bench, IR, K, D/ST)."""
    sp = (slot_position or "").upper()
    if sp in ("QB", "RB", "WR", "TE"):
        return sp
    if sp in ("OP", "QB/RB/WR/TE"):
        return "SUPERFLEX"
    if sp in ("FLEX", "RB/WR", "RB/WR/TE", "WR/TE"):
        return "FLEX"
    return None


def _league_settings(league: Any, lineup: list) -> dict[str, Any]:
    """Detect team count and starting-lineup composition so waiver replacement
    levels can be league-specific. Starter counts come from your own filled
    lineup (one entry per starting slot)."""
    team_count = getattr(getattr(league, "settings", None), "team_count", None)
    starters: dict[str, int] = {}
    for bp in lineup:
        bucket = _settings_slot(getattr(bp, "slot_position", None))
        if bucket:
            starters[bucket] = starters.get(bucket, 0) + 1
    return {"team_count": int(team_count or 12), "starters": starters}


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


def _weekly_projection(player: Any, week: int) -> float | None:
    """Best-effort weekly projection for a rostered player (used for other teams'
    rosters, which don't come through box scores). Falls back to the season
    projected average, then the season average."""
    stats = getattr(player, "stats", None)
    if isinstance(stats, dict):
        entry = stats.get(week) or stats.get(str(week))
        if isinstance(entry, dict) and entry.get("projected_points") is not None:
            try:
                return round(float(entry["projected_points"]), 2)
            except (TypeError, ValueError):
                pass
    for attr in ("projected_avg_points", "avg_points"):
        val = getattr(player, attr, None)
        if val is not None:
            try:
                return round(float(val), 2)
            except (TypeError, ValueError):
                pass
    return None


def _roster_player(player: Any, week: int, weekly: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Normalize a team.roster Player (any team in the league) for trade lookups."""
    return normalize_player(
        player_id=getattr(player, "playerId", getattr(player, "name", "?")),
        name=getattr(player, "name", "Unknown"),
        position=getattr(player, "position", None),
        pro_team=getattr(player, "proTeam", None),
        slot="rostered",
        injury_status=getattr(player, "injuryStatus", None),
        proj_points=_weekly_projection(player, week),
        avg_points=getattr(player, "avg_points", None),
        percent_owned=getattr(player, "percent_owned", None),
        ros_proj=_player_ros(player, week, weekly),
    )


def _find_my_box_lineup(
    league: Any, team_id: int | None, week: int
) -> tuple[Any, list, Any]:
    """Return (my_team, my_lineup, opponent_team) from this week's box scores.
    Opponent is None if my team is on bye (no matchup)."""
    box_scores = league.box_scores(week)
    for box in box_scores:
        home, away = box.home_team, box.away_team
        if team_id is not None and getattr(home, "team_id", None) == team_id:
            return home, box.home_lineup, away
        if team_id is not None and getattr(away, "team_id", None) == team_id:
            return away, box.away_lineup, home
    # No team_id match (or none configured): fall back to the first matchup.
    first = box_scores[0]
    return first.home_team, first.home_lineup, first.away_team


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
        team, lineup, opponent = _find_my_box_lineup(league, entry.team_id, week)
    except Exception as exc:
        return {**base, "error": f"ESPN box score error: {exc}"}

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

    # Pull the free-agent pool once (reused below).
    try:
        fa_objs = list(league.free_agents(size=60))
    except Exception:
        fa_objs = []

    roster = []
    for bp in lineup:
        pl = _box_player(bp, week)
        pl["recent_points"] = _recent_actual_points(bp, week)
        roster.append(pl)

    # Every team's roster, keyed by team name — powers trade evaluation. Already
    # loaded (used above for FA filtering), so no extra API calls.
    try:
        league_rosters = {
            (getattr(t, "team_name", None) or f"Team {getattr(t, 'team_id', '?')}"):
            [_roster_player(p, week) for p in t.roster]
            for t in league.teams
        }
    except Exception:
        league_rosters = {}

    try:
        free_agents = []
        for p in fa_objs:
            fa = _free_agent(p, week)
            if fa["id"] in rostered_ids:
                continue
            fa["recent_points"] = _recent_actual_points(p, week)
            free_agents.append(fa)
    except Exception:
        free_agents = []

    # Rest-of-season weekly projections: ESPN's API won't give us real future
    # weeks, so we compute a matchup-adjusted line per player (bye weeks = 0).
    _apply_ros_projections(
        [roster, *league_rosters.values(), free_agents], week, year
    )

    return {
        **base,
        "league_name": getattr(league, "settings", None)
        and getattr(league.settings, "name", None),
        "team_name": getattr(team, "team_name", "My Team"),
        "team_id": getattr(team, "team_id", None),
        "opponent": getattr(opponent, "team_name", None),
        "week": week,
        "season": str(year),
        "private": bool(espn_s2 and swid),
        "league_settings": _league_settings(league, lineup),
        "roster": roster,
        "free_agents": free_agents,
        "league_rosters": league_rosters,
    }
