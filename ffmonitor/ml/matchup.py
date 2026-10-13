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
def _schedule_map(year: int) -> dict[str, dict[int, dict]]:
    """{team: {week: {opp, venue, date}}} for the regular season. `venue` is the
    home team (whose stadium hosts the game); a missing week is a bye."""
    try:
        import nfl_data_py as nfl
        sched = nfl.import_schedules([year])
    except Exception:
        return {}
    out: dict[str, dict[int, dict]] = {}
    try:
        for _, g in sched.iterrows():
            if "game_type" in sched.columns and g.get("game_type") != "REG":
                continue
            try:
                wk = int(g.get("week"))
            except (TypeError, ValueError):
                continue
            home, away = _norm_team(g.get("home_team")), _norm_team(g.get("away_team"))
            date = str(g.get("gameday") or "")
            if home and away:
                out.setdefault(home, {})[wk] = {"opp": away, "venue": home, "date": date}
                out.setdefault(away, {})[wk] = {"opp": home, "venue": home, "date": date}
    except Exception:
        return {}
    return out


@lru_cache(maxsize=4)
def _weekly_odds(year: int, start_week: int, end_week: int) -> dict[int, dict]:
    """{week: {"teams": {team: {spread, implied}}, "avg_implied": float|None}}.

    Vegas only posts lines for the near term, so future weeks come back empty and
    the Vegas factor is a neutral 1.0 there — the matchup (defense) layer still
    applies. One fetch per week, cached across all leagues."""
    try:
        from ..odds import fetch_odds
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for w in range(start_week, end_week + 1):
        try:
            odds = fetch_odds(w, year)
        except Exception:
            odds = {}
        teams: dict[str, dict] = {}
        implieds: list[float] = []
        for abbr, info in (odds or {}).items():
            t = _norm_team(abbr)
            implied = info.get("implied_total")
            teams[t] = {"spread": info.get("spread"), "implied": implied}
            if implied is not None:
                implieds.append(float(implied))
        out[w] = {
            "teams": teams,
            "avg_implied": round(sum(implieds) / len(implieds), 2) if implieds else None,
        }
    return out


def _vegas_factor(odds_week: dict | None, team: str | None, pos: str) -> float:
    """Game-environment multiplier for one team-week: implied scoring total vs the
    slate average, times a position-specific game-script nudge from the spread."""
    if not odds_week:
        return 1.0
    info = (odds_week.get("teams") or {}).get(team)
    if not info:
        return 1.0
    factor = 1.0
    implied, avg = info.get("implied"), odds_week.get("avg_implied")
    if implied and avg and avg > 0:
        factor *= max(0.85, min(1.20, implied / avg))  # more scoring -> more points
    spread = info.get("spread")
    if spread is not None:
        s = max(-10.0, min(10.0, float(spread)))  # negative = favored
        if pos == "RB":
            factor *= 1.0 + (-s) * 0.010   # favorites run late -> RB boost
        else:                              # QB / WR / TE
            factor *= 1.0 + (s) * 0.008    # underdogs throw to catch up -> boost
    return max(0.80, min(1.25, factor))


def _dvp_from_season(year: int, through_week: int) -> dict[tuple[str, str], float]:
    """{(defense, position): factor} for one season: points that defense allows
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


@lru_cache(maxsize=8)
def _dvp_factors(year: int, through_week: int) -> dict[tuple[str, str], float]:
    """Defense-vs-position factors. Uses the current season once it has enough
    coverage; early in the year (sparse/unpublished data) it falls back to last
    season's full-year tendencies so opponent strength still varies week to week —
    the same prior real projection systems lean on before the new season settles."""
    current = _dvp_from_season(year, through_week)
    if len(current) >= 32:  # ~half the 64 (team x position) cells present
        return current
    prior = _dvp_from_season(year - 1, 18)
    return prior or current


def coverage(year: int, through_week: int) -> dict:
    """How much of each data layer actually loaded — for diagnosing flat output."""
    cur = _dvp_from_season(year, through_week)
    return {
        "dvp": len(_dvp_factors(year, through_week)),
        "dvp_current_season": len(cur),
        "dvp_source": "current" if len(cur) >= 32 else "prior-season",
        "schedule_teams": len(_schedule_map(year)),
    }


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
    odds = _weekly_odds(year, start_week, end_week)
    try:
        from . import weather
    except Exception:
        weather = None
    out: dict[str, float] = {}
    for w in range(start_week, end_week + 1):
        game = team_games.get(w)
        if not game:
            out[str(w)] = 0.0  # bye week
            continue
        dvp = factors.get((game["opp"], pos), 1.0)          # opponent defense
        vegas = _vegas_factor(odds.get(w), team_n, pos)      # scoring env + game script
        wx = weather.weather_factor(game.get("venue"), game.get("date"), pos) if weather else 1.0
        # Keep the combined swing within a believable band.
        combined = max(0.55, min(1.55, dvp * vegas * wx))
        out[str(w)] = round(base * combined, 2)
    return out or None
