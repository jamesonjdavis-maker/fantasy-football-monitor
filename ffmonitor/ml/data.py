"""Historical usage data layer, built on nfl_data_py.

Turns raw weekly NFL stats into one row per (player, season, week) with an
`opportunity share` and a *leakage-safe* trailing average of it. "Opportunity"
= targets + carries, the volume a player is fed regardless of whether it
converted to points yet — that's the signal that moves *before* fantasy output,
which is exactly the buy-low edge we're chasing.

Key anti-leakage rule enforced here: the trailing average for week W uses only
weeks strictly before W (via `.shift(1)`), and never bleeds across seasons.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from . import DEFAULT_POSITIONS

# Columns we want if the installed nfl_data_py version provides them. We select
# the intersection with what's actually returned, so a version that renames or
# drops a column degrades instead of crashing.
_WANTED_COLUMNS = [
    "player_id",
    "player_display_name",
    "position",
    "position_group",
    "recent_team",
    "season",
    "week",
    "season_type",
    "carries",
    "targets",
    "receptions",
    "fantasy_points_ppr",
    "target_share",  # provided by nfl_data_py (receiving only)
    "wopr",          # weighted opportunity rating (receiving only)
]


def _import_weekly(seasons: list[int]):
    """Fetch weekly data for the requested seasons, skipping any that aren't
    published yet.

    nflverse only hosts completed/in-progress seasons, so asking for a future
    season — or the current one during the first week or two before it's
    published — returns a 404. Rather than let that break the whole signal, we
    fetch season-by-season and use whatever actually exists.
    """
    try:
        import nfl_data_py as nfl
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "nfl_data_py/pandas are required for the ml module. "
            "Install them with: pip install -r requirements-ml.txt"
        ) from exc

    frames = []
    used: list[int] = []
    for yr in seasons:
        try:
            frames.append(nfl.import_weekly_data([yr]))
            used.append(yr)
        except Exception as exc:
            print(f"[ml] weekly data for {yr} unavailable ({exc}); skipping")

    if not frames:
        raise RuntimeError(
            f"no weekly data available for any of seasons {list(seasons)}"
        )
    print(f"[ml] weekly data loaded for seasons {used}")
    return pd.concat(frames, ignore_index=True)


@lru_cache(maxsize=8)
def _weekly_raw(seasons_key: tuple[int, ...]):
    return _import_weekly(list(seasons_key))


def build_usage_frame(
    seasons: Iterable[int],
    positions: Iterable[str] = DEFAULT_POSITIONS,
):
    """Return a DataFrame of per-player-week usage features.

    Columns: player_id, player, position, team, season, week,
             carries, targets, opportunity, opp_share,
             usage_trailing3, usage_delta, target_share, wopr, ppr
    """
    import pandas as pd  # local import keeps pandas optional for the core tool

    positions = {p.upper() for p in positions}
    df = _weekly_raw(tuple(seasons)).copy()

    # Keep only columns that actually exist in this nfl_data_py version.
    cols = [c for c in _WANTED_COLUMNS if c in df.columns]
    df = df[cols]

    # Regular season, skill positions, real teams only.
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["position"].isin(positions)]
    for c in ("carries", "targets", "receptions", "fantasy_points_ppr",
              "target_share", "wopr"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0

    # Opportunity = targets + carries (position-agnostic volume).
    df["opportunity"] = df["targets"] + df["carries"]

    # Team opportunity that week -> each player's share of it.
    team_opp = (
        df.groupby(["season", "week", "recent_team"])["opportunity"]
        .transform("sum")
    )
    df["opp_share"] = (df["opportunity"] / team_opp).where(team_opp > 0)

    # Leakage-safe trailing average: prior weeks only, within the same season.
    df = df.sort_values(["player_id", "season", "week"]).reset_index(drop=True)
    grp = df.groupby(["player_id", "season"])["opp_share"]
    df["usage_trailing3"] = grp.transform(
        lambda s: s.shift(1).rolling(window=3, min_periods=2).mean()
    )

    # The signal: how much this week's share exceeds the recent baseline.
    df["usage_delta"] = df["opp_share"] - df["usage_trailing3"]

    df = df.rename(
        columns={
            "player_display_name": "player",
            "recent_team": "team",
            "fantasy_points_ppr": "ppr",
        }
    )
    keep = [
        "player_id", "player", "position", "team", "season", "week",
        "carries", "targets", "opportunity", "opp_share",
        "usage_trailing3", "usage_delta", "target_share", "wopr", "ppr",
    ]
    return df[[c for c in keep if c in df.columns]]


def latest_season_week(frame) -> tuple[int, int]:
    """Most recent (season, week) present in the frame."""
    season = int(frame["season"].max())
    week = int(frame[frame["season"] == season]["week"].max())
    return season, week
