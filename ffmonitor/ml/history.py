"""Historical calibration for the rising-production signal.

Pulls many completed NFL seasons from nfl_data_py and derives, per position, how
large a jump in recent fantasy production is actually "notable" — a data-driven
threshold instead of a hand-picked guess. The current-season signal (measured
from ESPN, same unit: fantasy points) flags players whose recent trend clears
that historical bar.

More seasons -> steadier thresholds. Controlled by HISTORY_SEASONS (env) or the
n_seasons argument. Completed seasons only — the current season is excluded
because it's partial (and may not be published yet).
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

# Positions we calibrate. IDP/K/DST breakouts aren't usage-driven the same way.
_POSITIONS = ("RB", "WR", "TE", "QB")
# Ignore deep-bench noise: only weeks where the player was a real contributor.
_MIN_ROLLING_PPG = 4.0


def _completed_seasons(n: int) -> list[int]:
    """The n most-recent *completed* seasons (current season excluded)."""
    today = date.today()
    current = today.year if today.month >= 8 else today.year - 1
    return list(range(current - n, current))


# Default "last startable" rank per position for a standard 12-team league
# (1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX). Used when a league's settings are unknown.
_DEFAULT_RANKS = {"QB": 14, "RB": 29, "WR": 29, "TE": 14}


def ranks_from_settings(team_count: int, starters: dict[str, int]) -> dict[str, int]:
    """Convert a league's team count + starting-lineup counts into a replacement
    rank per position. FLEX demand is split across RB/WR/TE; a SUPERFLEX/OP slot
    adds QB demand. rank ≈ teams × (dedicated starters + share of flex)."""
    flex = starters.get("FLEX", 0)
    sflex = starters.get("SUPERFLEX", 0)

    def r(base: float, extra: float) -> int:
        return max(1, round(team_count * (base + extra)))

    return {
        "QB": r(starters.get("QB", 1), sflex * 0.5),
        "RB": r(starters.get("RB", 2), flex * 0.4),
        "WR": r(starters.get("WR", 2), flex * 0.4),
        "TE": r(starters.get("TE", 1), flex * 0.2),
    }


@lru_cache(maxsize=16)
def _levels_for_ranks(ranks_items: tuple, n_seasons: int | None = None) -> dict:
    import pandas as pd

    from .data import _weekly_raw

    ranks = dict(ranks_items)
    n = n_seasons or default_n_seasons()
    df = _weekly_raw(tuple(_completed_seasons(n))).copy()
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["position"].isin(_POSITIONS)].copy()
    df["ppr"] = pd.to_numeric(df.get("fantasy_points_ppr", 0), errors="coerce").fillna(0.0)

    levels: dict[str, float] = {}
    for pos, rank in ranks.items():
        pos_df = df[df["position"] == pos]

        def _at_rank(group) -> float:
            ranked = group["ppr"].sort_values(ascending=False).to_numpy()
            if len(ranked) == 0:
                return 0.0
            return float(ranked[min(rank - 1, len(ranked) - 1)])

        per_week = pos_df.groupby(["season", "week"]).apply(_at_rank)
        levels[pos] = round(float(per_week.mean()) if len(per_week) else 0.0, 1)
    return levels


def replacement_levels(ranks: dict[str, int] | None = None) -> dict:
    """Per-position replacement level in weekly PPG for the given ranks (or the
    12-team default). Subtracting it makes points comparable across positions."""
    ranks = ranks or _DEFAULT_RANKS
    return _levels_for_ranks(tuple(sorted(ranks.items())))


def default_n_seasons() -> int:
    raw = os.getenv("HISTORY_SEASONS", "").strip()
    try:
        n = int(raw)
        return max(1, min(n, 25))  # clamp to something sane
    except ValueError:
        return 8


@lru_cache(maxsize=4)
def fantasy_jump_thresholds(
    n_seasons: int | None = None, percentile: float = 0.75
) -> dict:
    """Per-position threshold for a 'notable' week-over-week rise in 3-week
    rolling fantasy PPG, taken as the given percentile of historical positive
    rises. Returns {'seasons': [...], 'percentile': p, 'thresholds': {pos: pts}}.
    """
    import pandas as pd

    from .data import _weekly_raw

    n = n_seasons or default_n_seasons()
    seasons = _completed_seasons(n)
    # Shared, cached fetch — reused by montecarlo.py in the same run.
    df = _weekly_raw(tuple(seasons)).copy()  # resilient: skips seasons that 404

    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["position"].isin(_POSITIONS)].copy()
    df["ppr"] = pd.to_numeric(df.get("fantasy_points_ppr", 0), errors="coerce").fillna(0.0)

    df = df.sort_values(["player_id", "season", "week"])
    grp = df.groupby(["player_id", "season"])["ppr"]
    # Recent form (3-week rolling PPG) vs the player's season-to-date average.
    # delta > 0 means they're producing above their own baseline lately — the
    # same thing we can measure live from ESPN (recent PPG vs avg_points).
    df["roll3"] = grp.transform(lambda s: s.rolling(3, min_periods=2).mean())
    df["season_avg"] = grp.transform(lambda s: s.expanding().mean())
    df["delta"] = df["roll3"] - df["season_avg"]

    notable = df[(df["roll3"] >= _MIN_ROLLING_PPG) & (df["delta"] > 0)]
    thresholds = (
        notable.groupby("position")["delta"].quantile(percentile).round(2).to_dict()
    )
    # Guarantee every position has a value even if data was thin.
    for pos in _POSITIONS:
        thresholds.setdefault(pos, 3.0)

    return {
        "seasons": [int(s) for s in df["season"].unique().tolist()],
        "percentile": percentile,
        "thresholds": {k: float(v) for k, v in thresholds.items()},
    }
