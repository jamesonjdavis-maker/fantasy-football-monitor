"""Matchup-adjusted rest-of-season weekly projections.

ESPN's API only exposes a player's current-week projection and a season-long
average, not a real week-by-week forecast. To show a varying weekly line we
build our own: take the player's per-game baseline and scale each remaining week
by how that week's opponent defends the player's position (defense-vs-position),
with bye weeks as 0. These are clearly our estimates, not ESPN's numbers.

Both inputs come from nflverse (the same source the rest of the ML uses):
  * import_schedules -> who each NFL team plays each week (and byes)
  * weekly player stats -> fantasy points each defense allows per position

Everything degrades to None if nflverse isn't available, so the caller can fall
back; the heavy loads are cached so they run once per daily snapshot.
"""

from __future__ import annotations

from functools import lru_cache

# ESPN proTeam abbreviations that differ from nflverse's.
_TEAM_FIX = {
    "WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR",
}
_POSITIONS = ("QB", "RB", "WR", "TE")
# Keep matchup swings sensible — a defense can't make a star worthless or elite.
_FACTOR_MIN, _FACTOR_MAX = 0.75, 1.30


def _norm_team(team: str | None) -> str | None:
    if not team:
        return None
    t = str(team).upper()
    return _TEAM_FIX.get(t, t)


@lru_cache(maxsize=4)
def _schedule_map(year: int) -> dict[str, dict[int, str]]:
    """{team: {week: opponent}} for the regular season (missing week == bye)."""
    try:
        import nfl_data_py as nfl
        sched = nfl.import_schedules([year])
    except Exception:
        return {}
    out: dict[str, dict[int, str]] = {}
    try:
        for _, g in sched.iterrows():
            if "game_type" in sched.columns and g.get("game_type") != "REG":
                continue
            try:
                wk = int(g.get("week"))
            except (TypeError, ValueError):
                continue
            home, away = _norm_team(g.get("home_team")), _norm_team(g.get("away_team"))
            if home and away:
                out.setdefault(home, {})[wk] = away
                out.setdefault(away, {})[wk] = home
    except Exception:
        return {}
    return out


@lru_cache(maxsize=8)
def _dvp_factors(year: int, through_week: int) -> dict[tuple[str, str], float]:
    """{(defense, position): factor} where factor = points that defense allows
    to the position per game, divided by the league average for that position."""
    try:
        import pandas as pd
        from .data import _weekly_raw
        df = _weekly_raw((year,)).copy()
    except Exception:
        return {}
    needed = {"opponent_team", "position", "fantasy_points_ppr", "week"}
    if not needed.issubset(df.columns):
        return {}
    try:
        if "season_type" in df.columns:
            df = df[df["season_type"] == "REG"]
        df = df[(df["week"] <= through_week) & (df["position"].isin(_POSITIONS))]
        df["fantasy_points_ppr"] = pd.to_numeric(
            df["fantasy_points_ppr"], errors="coerce"
        ).fillna(0.0)
        agg = (
            df.groupby(["opponent_team", "position"])
            .agg(pts=("fantasy_points_ppr", "sum"), games=("week", "nunique"))
            .reset_index()
        )
        agg = agg[agg["games"] > 0]
        agg["perg"] = agg["pts"] / agg["games"]
        league = agg.groupby("position")["perg"].mean().to_dict()
        factors: dict[tuple[str, str], float] = {}
        for _, r in agg.iterrows():
            avg = league.get(r["position"])
            if avg and avg > 0:
                f = max(_FACTOR_MIN, min(_FACTOR_MAX, r["perg"] / avg))
                factors[(_norm_team(r["opponent_team"]), r["position"])] = round(f, 3)
        return factors
    except Exception:
        return {}


def build_ros(
    base: float | None,
    position: str | None,
    team: str | None,
    start_week: int,
    end_week: int,
    year: int,
    through_week: int,
) -> dict[str, float] | None:
    """Matchup-adjusted {week: projected_points} from start_week..end_week.

    Returns None when there's no schedule to build from (caller falls back).
    A week the team doesn't play is a bye (0). If defense data is missing, the
    factor is a neutral 1.0 so you still get the schedule with byes."""
    if base is None or base <= 0 or not position or not team:
        return None
    schedule = _schedule_map(year)
    team_n = _norm_team(team)
    team_games = schedule.get(team_n or "")
    if not team_games:
        return None
    pos = str(position).upper()
    factors = _dvp_factors(year, through_week)
    out: dict[str, float] = {}
    for w in range(start_week, end_week + 1):
        opp = team_games.get(w)
        if opp is None:
            out[str(w)] = 0.0  # bye week
            continue
        factor = factors.get((opp, pos), 1.0)
        out[str(w)] = round(base * factor, 2)
    return out or None
