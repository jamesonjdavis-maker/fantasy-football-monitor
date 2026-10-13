"""Forward-looking, player- and team-level adjustments to the scoring baseline.

The baseline (recency-weighted fantasy points) already reflects a player's
realized volume and efficiency. These factors capture what the raw points can't:

  * Volume / role trend - is the player's opportunity (touches/targets) rising or
    falling lately versus their season? Role changes lead points, so this is
    genuinely forward-looking.
  * Efficiency regression - a player scoring well above the per-opportunity norm
    for their position is likely riding unsustainable efficiency (e.g. a TD-heavy
    start); pull them partway back toward what their volume supports (and lift the
    unlucky).
  * Team pace / pass rate - a fast or pass-heavy offense creates more opportunity
    for its pass catchers (and a run-heavy one for its backs).

All are small, clamped nudges applied to the baseline level (not per week), so
they don't fight the matchup/Vegas/weather layers or double-count the points that
already contain them. Everything degrades to 1.0 if nflverse data is missing.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .matchup import _norm_team

_POSITIONS = ("QB", "RB", "WR", "TE")
# Minimum per-game opportunity to count toward a position's efficiency norm.
_OPP_FLOOR = {"QB": 15, "RB": 5, "WR": 3, "TE": 2}


def _norm(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFD", str(name).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[.'']", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _opportunity(pos, carries, targets, attempts):
    if pos == "QB":
        return attempts + carries
    if pos == "RB":
        return carries + targets
    return targets  # WR / TE


@lru_cache(maxsize=8)
def build_player_factors(year: int, through_week: int) -> dict:
    """{"players": {norm_name: {vol, eff, pos}}, "teams": {team: {pos: factor}}}."""
    empty = {"players": {}, "teams": {}}
    try:
        import pandas as pd
        from .data import _weekly_raw
        df = _weekly_raw((year,)).copy()
    except Exception:
        return empty
    need = {"player_display_name", "position", "recent_team", "week", "fantasy_points_ppr"}
    if not need.issubset(df.columns):
        return empty
    try:
        if "season_type" in df.columns:
            df = df[df["season_type"] == "REG"]
        df = df[(df["week"] <= through_week) & (df["position"].isin(_POSITIONS))].copy()
        for c in ("carries", "targets", "attempts", "fantasy_points_ppr"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
        df["opp"] = [
            _opportunity(p, c, t, a)
            for p, c, t, a in zip(df["position"], df["carries"], df["targets"], df["attempts"])
        ]

        # Per-player season aggregates.
        agg = (
            df.groupby(["player_display_name", "position"])
            .agg(games=("week", "nunique"), opp=("opp", "sum"), ppr=("fantasy_points_ppr", "sum"))
            .reset_index()
        )
        agg = agg[agg["games"] >= 2]
        agg["opp_pg"] = agg["opp"] / agg["games"]
        agg["ppo"] = (agg["ppr"] / agg["opp"]).where(agg["opp"] > 0)

        # Position efficiency norms (median points-per-opportunity among regulars).
        norms = {}
        for pos in _POSITIONS:
            sub = agg[(agg["position"] == pos) & (agg["opp_pg"] >= _OPP_FLOOR[pos])]
            vals = sub["ppo"].dropna()
            if len(vals):
                norms[pos] = float(vals.median())

        # Recent (last 3 games) opportunity per player, for the role trend.
        recent = (
            df.sort_values(["player_display_name", "week"])
            .groupby("player_display_name")["opp"]
            .apply(lambda s: s.tail(3).mean())
            .to_dict()
        )

        players = {}
        for r in agg.itertuples(index=False):
            vol = 1.0
            rec_pg = recent.get(r.player_display_name)
            if r.opp_pg and r.opp_pg > 0 and rec_pg is not None:
                vol = max(0.85, min(1.20, rec_pg / r.opp_pg))
            eff = 1.0
            if r.position in norms and r.ppo and r.ppo > 0:
                # Regress 35% of the way from the player's rate toward the norm.
                eff = max(0.85, min(1.15, 0.65 + 0.35 * (norms[r.position] / r.ppo)))
            players[_norm(r.player_display_name)] = {
                "vol": round(vol, 3), "eff": round(eff, 3), "pos": r.position,
            }

        # Team pace and pass rate.
        teamw = (
            df.groupby(["recent_team", "week"])
            .agg(pass_att=("attempts", "sum"), rush_att=("carries", "sum"))
            .reset_index()
        )
        teamw["plays"] = teamw["pass_att"] + teamw["rush_att"]
        tz = (
            teamw.groupby("recent_team")
            .agg(plays=("plays", "mean"), pass_att=("pass_att", "mean"))
            .reset_index()
        )
        tz["passrate"] = (tz["pass_att"] / tz["plays"]).where(tz["plays"] > 0)
        lg_plays = float(tz["plays"].mean()) if len(tz) else 0.0
        lg_pass = float(tz["passrate"].mean()) if len(tz) else 0.0

        teams = {}
        for r in tz.itertuples(index=False):
            pace = max(0.92, min(1.08, r.plays / lg_plays)) if lg_plays else 1.0
            pr = r.passrate
            if lg_pass and pr is not None and 0 < lg_pass < 1:
                passf = max(0.90, min(1.12, pr / lg_pass))
                rushf = max(0.90, min(1.12, (1 - pr) / (1 - lg_pass)))
            else:
                passf = rushf = 1.0
            clamp = lambda f: round(max(0.88, min(1.15, f)), 3)
            teams[_norm_team(r.recent_team)] = {
                "QB": clamp(pace * passf), "WR": clamp(pace * passf),
                "TE": clamp(pace * passf), "RB": clamp(pace * rushf),
            }
        return {"players": players, "teams": teams}
    except Exception:
        return empty


def level_factor(name: str | None, team: str | None, position: str | None,
                 year: int, through_week: int) -> float:
    """Combined baseline multiplier for a player: volume-trend x efficiency x team
    context, clamped. 1.0 when we can't match the player."""
    data = build_player_factors(year, through_week)
    pos = str(position).upper() if position else None
    factor = 1.0
    pl = data["players"].get(_norm(name))
    if pl and (pos is None or pl.get("pos") == pos):
        factor *= pl["vol"] * pl["eff"]
    tm = data["teams"].get(_norm_team(team))
    if tm and pos:
        factor *= tm.get(pos, 1.0)
    return round(max(0.75, min(1.30, factor)), 3)
