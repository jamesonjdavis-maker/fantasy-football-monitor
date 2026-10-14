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

# ============================================================================
# PROJECTION TUNING  -  the judgment call in building a player's per-game
# baseline: how much to trust ESPN's forward-looking projected average versus the
# player's own realized form. These two should add up to 1.0. Everything else in
# the projection (defense-vs-position, Vegas, weather, level factors) is a
# measured input, not a weight. The trade-DECISION weights (upside tilt, bench
# depth, grade cutoffs, trade-finder knobs) live separately in
# discord-bot/worker.js under TRADE_TUNING. See README sections 6 and 11.
# ============================================================================
ESPN_PROJ_WEIGHT = 0.75      # trust in ESPN's projected average (role/depth/injury-aware)
REALIZED_FORM_WEIGHT = 0.25  # trust in the player's own recent scoring form


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


def _proj_meta(week: int, year: int) -> dict:
    """Small diagnostic of which projection layers actually loaded their data."""
    through = max(1, week - 1)
    meta: dict = {"week": week, "year": year}
    try:
        from ..ml import matchup
        meta.update(matchup.coverage(year, through))
    except Exception as exc:
        meta["matchup_error"] = str(exc)[:120]
    try:
        from ..ml import playerfactors
        pf = playerfactors.build_player_factors(year, through)
        meta["player_factors"] = len(pf.get("players", {}))
        meta["team_factors"] = len(pf.get("teams", {}))
    except Exception as exc:
        meta["playerfactors_error"] = str(exc)[:120]
    # Probe import_weekly_data DIRECTLY so we see the real per-year exception
    # (the ml loader swallows it behind a generic "no weekly data" message).
    for label, yr in (("current", year), ("prior", year - 1)):
        try:
            import nfl_data_py as nfl
            df = nfl.import_weekly_data([yr])
            meta[f"weekly_rows_{label}"] = int(len(df))
            meta[f"weekly_has_opp_{label}"] = "opponent_team" in df.columns
        except Exception as exc:
            meta[f"weekly_error_{label}"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return meta


def _realized_form(p: dict) -> float | None:
    """The player's own realized per-game form: recent weeks if we have them, else
    the season average. None if there's nothing to go on."""
    recent = [x for x in (p.get("recent_points") or []) if isinstance(x, (int, float))]
    if recent:
        return sum(recent) / len(recent)
    avg = p.get("avg_points") or 0
    return avg if avg > 0 else None


def _baseline_pergame(p: dict) -> float | None:
    """A player's per-game scoring baseline for the rest of the season. Blends
    ESPN's forward-looking projected average (role/depth/injury-aware, doesn't lag
    a slow start) with the player's own realized form (weights in the PROJECTION
    TUNING block above), so a big gap between projection and reality gets pulled
    partway toward what's actually happening, without gutting a genuine bounce-back.
    Falls back to realized form, then this week's projection, when ESPN's average
    isn't available."""
    proj_avg = p.get("proj_avg") or 0
    if proj_avg > 0:
        form = _realized_form(p)
        if form and form > 0:
            return round(ESPN_PROJ_WEIGHT * proj_avg + REALIZED_FORM_WEIGHT * form, 2)
        return round(float(proj_avg), 2)
    form = _realized_form(p)
    if form and form > 0:
        return round(form, 2)
    return p.get("proj_points")


# How a known injury status discounts upcoming weeks: (weeks_affected, factor).
# Short-term designations (out/doubtful/questionable) touch only the current week,
# since the player could be back the next. Season-long ones (IR, PUP, suspension)
# are known multi-week absences, so they zero the minimum stretch a player is out
# (IR is at least four games); we don't guess an exact return beyond that.
_INJURY_IMPACT = {
    "OUT": (1, 0.0),
    "DOUBTFUL": (1, 0.35),
    "QUESTIONABLE": (1, 0.9),
    "SUSPENDED": (2, 0.0),
    "IR": (4, 0.0),
    "PUP": (4, 0.0),
}


def _apply_ros_projections(player_groups: list[list[dict]], week: int, year: int) -> None:
    """Overwrite each player's ros_proj with a matchup-adjusted weekly line: a
    recency-weighted per-game baseline scaled by opponent defense-vs-position, the
    Vegas game environment, and weather, byes = 0. A known injury status discounts
    the current week. Best-effort: if data can't load, players keep their
    fallback."""
    try:
        from ..ml import matchup
    except Exception:
        return
    try:
        from ..ml import playerfactors
    except Exception:
        playerfactors = None
    through = max(1, week - 1)  # defense stats through completed weeks only
    wk = str(week)
    # My own roster comes from box scores, which may not carry projected_avg_points;
    # backfill it from the league-roster/free-agent copies that do, by name.
    have_avg: dict[str, float] = {}
    for group in player_groups:
        for p in group:
            pa = p.get("proj_avg")
            if pa and pa > 0:
                have_avg[(p.get("name") or "").lower().strip()] = pa
    for group in player_groups:
        for p in group:
            if not (p.get("proj_avg") and p["proj_avg"] > 0):
                pa = have_avg.get((p.get("name") or "").lower().strip())
                if pa:
                    p["proj_avg"] = pa
    for group in player_groups:
        for p in group:
            base = _baseline_pergame(p)
            # Role trend, efficiency regression, and team pace/pass rate only help
            # when the baseline is REALIZED points (they add the forward-looking
            # view). ESPN's projected average already bakes all of that in, so
            # applying them there would double-count and inflate — skip them then.
            uses_espn_proj = bool(p.get("proj_avg") and p["proj_avg"] > 0)
            if base and playerfactors and not uses_espn_proj:
                base = round(base * playerfactors.level_factor(
                    p.get("name"), p.get("pro_team"), p.get("position"), year, through
                ), 2)
            ros = matchup.build_ros(
                base, p.get("position"), p.get("pro_team"),
                week, _SEASON_END_WEEK, year, through,
            )
            if not ros:
                continue
            impact = _INJURY_IMPACT.get(p.get("injury_status"))
            if impact:
                weeks_out, factor = impact
                for i in range(weeks_out):
                    key = str(week + i)
                    if key in ros:
                        ros[key] = round(ros[key] * factor, 2)
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
        proj_avg=getattr(bp, "projected_avg_points", None),
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
        proj_avg=getattr(p, "projected_avg_points", None),
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
        proj_avg=getattr(player, "projected_avg_points", None),
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
    proj_meta = _proj_meta(week, year)

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
        "proj_meta": proj_meta,
    }
