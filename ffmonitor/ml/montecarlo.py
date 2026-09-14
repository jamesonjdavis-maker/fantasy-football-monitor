"""Monte Carlo floor/ceiling projections.

A single projected number hides risk: 12 projected points could mean a rock-safe
10–14 range, or a boom/bust 3–28. This turns each projection into a distribution.

Method:
  1. From many completed seasons, learn each position's *performance multiplier*
     distribution — weekly fantasy points divided by that player's season
     average. This is a dimensionless boom/bust shape (WRs swing harder than RBs)
     that we can transfer onto any player's current projection.
  2. For a player projected for `mu` points, simulate N outcomes = mu × sampled
     multipliers, and read off the 10th / 50th / 90th percentiles as
     floor / median / ceiling.

Needs numpy + pandas (requirements-ml.txt). History (nfl_data_py) is only used
to shape the variance; the projection itself comes from ESPN, live.
"""

from __future__ import annotations

from functools import lru_cache

_POSITIONS = ("RB", "WR", "TE", "QB")
_MIN_SEASON_AVG = 6.0  # only shape variance from real contributors
_N_SIMS = 5000


def _tier(avg: float | None) -> str:
    """Bucket a player by scoring level. Lower-scored players are relatively
    boomier (more zeros, occasional spikes); studs are steadier — so variance is
    shaped per tier, not just per position."""
    if avg is None:
        return "mid"
    if avg < 9:
        return "low"
    if avg < 14:
        return "mid"
    return "high"


@lru_cache(maxsize=2)
def _multipliers(n_seasons: int | None = None) -> dict:
    """(position, tier) -> array of (weekly points / season average), plus a
    per-position pooled fallback keyed (position, 'all')."""
    import numpy as np
    import pandas as pd

    from .data import _weekly_raw
    from .history import _completed_seasons, default_n_seasons

    n = n_seasons or default_n_seasons()
    # Shared, cached fetch — reused by history.py in the same run (one download).
    df = _weekly_raw(tuple(_completed_seasons(n))).copy()
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["position"].isin(_POSITIONS)].copy()
    df["ppr"] = pd.to_numeric(df.get("fantasy_points_ppr", 0), errors="coerce").fillna(0.0)
    df["season_avg"] = df.groupby(["player_id", "season"])["ppr"].transform("mean")
    df = df[df["season_avg"] >= _MIN_SEASON_AVG]
    df["mult"] = df["ppr"] / df["season_avg"]
    df["tier"] = df["season_avg"].apply(_tier)

    out: dict[tuple[str, str], "np.ndarray"] = {}
    for pos in _POSITIONS:
        pos_df = df[df["position"] == pos]
        out[(pos, "all")] = pos_df["mult"].to_numpy()
        for tier in ("low", "mid", "high"):
            arr = pos_df.loc[pos_df["tier"] == tier, "mult"].to_numpy()
            if len(arr) >= 200:  # enough to be meaningful
                out[(pos, tier)] = arr[np.isfinite(arr)]
    return out


def simulate(projection: float | None, position: str | None, seed: int | None = None):
    """Return {proj, floor, median, ceiling, label} for a projection, or None if
    it can't be simulated (no projection, or position we don't model)."""
    import numpy as np

    if projection is None or projection <= 0:
        return None
    pos = (position or "").upper()
    dist = _multipliers()
    mults = dist.get((pos, _tier(projection)))
    if mults is None or len(mults) == 0:
        mults = dist.get((pos, "all"))
    if mults is None or len(mults) == 0:
        return None

    rng = np.random.default_rng(seed)
    sims = projection * rng.choice(mults, size=_N_SIMS)
    floor, median, ceiling = (float(x) for x in np.percentile(sims, [10, 50, 90]))
    result = {
        "proj": round(float(projection), 1),
        "floor": round(floor, 1),
        "median": round(median, 1),
        "ceiling": round(ceiling, 1),
    }
    result["label"] = _label(result)
    return result


def _label(r: dict) -> str:
    """Classify the risk shape: safe floor vs boom/bust vs balanced."""
    med = r["median"] or 0.01
    spread = (r["ceiling"] - r["floor"]) / med
    if spread >= 1.6:
        return "boom/bust"
    if r["floor"] / med >= 0.65:
        return "safe floor"
    return "balanced"


def attach_ranges(snapshot: dict) -> None:
    """Mutate the snapshot in place: add proj_floor/median/ceiling/label to every
    roster player we can simulate. Uses the ESPN weekly projection (proj_points),
    falling back to season average."""
    for snap in snapshot.get("platforms", {}).values():
        if not isinstance(snap, dict) or not snap.get("enabled"):
            continue
        for player in snap.get("roster", []):
            mu = player.get("proj_points")
            if mu is None or mu <= 0:
                mu = player.get("avg_points")
            sim = simulate(mu, player.get("position"))
            if sim:
                player["proj_floor"] = sim["floor"]
                player["proj_median"] = sim["median"]
                player["proj_ceiling"] = sim["ceiling"]
                player["proj_label"] = sim["label"]
