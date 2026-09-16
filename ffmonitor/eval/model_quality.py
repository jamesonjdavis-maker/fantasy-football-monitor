"""Model-quality metrics on historical seasons (pure ML, no live season needed).

Three checks on the projections and signals themselves:

  1. projection_mae — how far off are the projections? Compared against a
     season-average baseline and a naive last-week baseline.
  2. calibration    — do actual outcomes land inside the Monte Carlo P10–P90
     ("80%") floor/ceiling band about 80% of the time?
  3. heating_up     — of players the "heating up" signal flags, what share
     actually sustained above-baseline production over the next 2 weeks
     (precision), versus the base rate?

Everything is leakage-safe: features use only weeks strictly before the graded
week. Reuses the loader/feature prep from backtest.py.

Run:  python -m ffmonitor.eval.model_quality [start end]
"""

from __future__ import annotations

import sys

from .backtest import _POSITIONS, _completed_seasons, load_weekly, prepare

_MIN_CONTRIB = 4.0        # only grade players who are real contributors
_MIN_SEASON_AVG = 6.0     # multiplier distribution: real contributors only


# --------------------------------------------------------------------------- #
# 1. Projection error
# --------------------------------------------------------------------------- #
def projection_mae(df) -> dict:
    """Mean absolute error of each projection vs actual weekly points."""
    d = df.dropna(subset=["proj", "ttd", "lw"]).copy()
    d = d[d["proj"] >= _MIN_CONTRIB]  # rosterable/startable players
    if d.empty:
        return {"n": 0}
    err = lambda col: (d[col] - d["ppr"]).abs().mean()
    mae_model = err("proj")       # recency-weighted season average
    mae_savg = err("ttd")         # season-to-date average
    mae_lw = err("lw")            # last week's points (naive)
    return {
        "n": int(len(d)),
        "mae_model": round(float(mae_model), 2),
        "mae_seasonavg": round(float(mae_savg), 2),
        "mae_lastweek": round(float(mae_lw), 2),
        "vs_seasonavg_pct": round((mae_savg - mae_model) / mae_savg * 100, 1),
        "vs_lastweek_pct": round((mae_lw - mae_model) / mae_lw * 100, 1),
    }


# --------------------------------------------------------------------------- #
# 2. Monte Carlo calibration
# --------------------------------------------------------------------------- #
def _tier(avg: float) -> str:
    if avg < 9:
        return "low"
    if avg < 14:
        return "mid"
    return "high"


def _multiplier_percentiles(df) -> dict:
    """Per (position, tier) 10th/90th percentiles of the weekly performance
    multiplier (weekly pts ÷ season average). Because simulated outcomes are
    projection × multiplier, the P10/P90 of the outcome equal projection × the
    P10/P90 of the multiplier — so we can read floor/ceiling analytically."""
    import numpy as np

    m = df.copy()
    m["season_avg_full"] = m.groupby(["player_id", "season"])["ppr"].transform("mean")
    m = m[m["season_avg_full"] >= _MIN_SEASON_AVG]
    m["mult"] = m["ppr"] / m["season_avg_full"]
    m["tier"] = m["season_avg_full"].apply(_tier)

    pct: dict = {}
    for pos in _POSITIONS:
        pos_m = m[m["position"] == pos]["mult"].to_numpy()
        if len(pos_m):
            pct[(pos, "all")] = (float(np.percentile(pos_m, 10)),
                                 float(np.percentile(pos_m, 90)))
        for tier in ("low", "mid", "high"):
            arr = m[(m["position"] == pos) & (m["tier"] == tier)]["mult"].to_numpy()
            if len(arr) >= 200:
                pct[(pos, tier)] = (float(np.percentile(arr, 10)),
                                    float(np.percentile(arr, 90)))
    return pct


def calibration(df) -> dict:
    """Share of actual outcomes that fall within the predicted P10–P90 band.
    Well-calibrated ⇒ ~80% coverage (with ~10% below floor, ~10% above ceiling)."""
    pct = _multiplier_percentiles(df)
    d = df.dropna(subset=["proj"]).copy()
    d = d[d["proj"] >= _MIN_CONTRIB]
    if d.empty:
        return {"n": 0}

    inside = below = above = 0
    n = 0
    for _, r in d.iterrows():
        pos = r["position"]
        p = pct.get((pos, _tier(r["proj"]))) or pct.get((pos, "all"))
        if not p:
            continue
        floor, ceiling = r["proj"] * p[0], r["proj"] * p[1]
        actual = r["ppr"]
        n += 1
        if actual < floor:
            below += 1
        elif actual > ceiling:
            above += 1
        else:
            inside += 1
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "coverage_pct": round(inside / n * 100, 1),   # target ≈ 80
        "below_floor_pct": round(below / n * 100, 1),  # target ≈ 10
        "above_ceiling_pct": round(above / n * 100, 1),  # target ≈ 10
    }


# --------------------------------------------------------------------------- #
# 3. Heating-up precision
# --------------------------------------------------------------------------- #
def heating_up_precision(df) -> dict:
    """Flag players whose recent form (roll3) exceeds their season baseline by a
    position-specific bar (75th pct of positive deltas), then check whether they
    sustained above-baseline production over the next 2 weeks."""
    import numpy as np
    import pandas as pd

    d = df.copy()
    d["delta"] = d["roll3"] - d["ttd"]
    # Next-2-week outcome window.
    g = d.groupby(["player_id", "season"])["ppr"]
    fut = pd.DataFrame({i: g.shift(-i) for i in (1, 2)})
    d["fut2"] = fut.mean(axis=1)
    d["fut2_n"] = fut.notna().sum(axis=1)

    elig = d.dropna(subset=["roll3", "ttd"]).copy()
    elig = elig[(elig["roll3"] >= _MIN_CONTRIB) & (elig["fut2_n"] >= 1)]
    if elig.empty:
        return {"n": 0}

    # Position bar = 75th percentile of positive deltas (historical "notable rise").
    pos_bar = {}
    for pos in _POSITIONS:
        pos_deltas = elig[(elig["position"] == pos) & (elig["delta"] > 0)]["delta"]
        pos_bar[pos] = float(np.percentile(pos_deltas, 75)) if len(pos_deltas) else 3.0

    elig["bar"] = elig["position"].map(pos_bar)
    # "Sustained" = next-2-week PPG stays above the player's own season baseline.
    elig["sustained"] = elig["fut2"] > elig["ttd"]

    flagged = elig[elig["delta"] > elig["bar"]]
    if flagged.empty:
        return {"n": 0}
    precision = flagged["sustained"].mean()
    base = elig["sustained"].mean()
    return {
        "n": int(len(flagged)),
        "precision_pct": round(float(precision) * 100, 1),
        "base_rate_pct": round(float(base) * 100, 1),
    }


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(seasons: list[int] | None = None) -> dict:
    seasons = seasons or _completed_seasons(6)
    print(f"Model-quality metrics, seasons {seasons[0]}–{seasons[-1]} …")
    df = prepare(load_weekly(seasons))

    mae = projection_mae(df)
    cal = calibration(df)
    heat = heating_up_precision(df)

    print("\n=== RESULTS ===")
    if mae.get("n"):
        print(f"[projection] MAE {mae['mae_model']} pts | season-avg {mae['mae_seasonavg']} "
              f"| last-week {mae['mae_lastweek']}  → {mae['vs_seasonavg_pct']}% better than "
              f"season-avg, {mae['vs_lastweek_pct']}% better than last-week ({mae['n']:,} wks)")
    if cal.get("n"):
        print(f"[calibration] {cal['coverage_pct']}% of outcomes inside the 80% "
              f"floor–ceiling band (target ~80) | {cal['below_floor_pct']}% below floor, "
              f"{cal['above_ceiling_pct']}% above ceiling ({cal['n']:,} predictions)")
    if heat.get("n"):
        print(f"[heating-up] {heat['precision_pct']}% of flagged players sustained "
              f"above-baseline production next 2 wks vs {heat['base_rate_pct']}% base "
              f"rate ({heat['n']:,} flags)")

    return {"seasons": seasons, "projection": mae, "calibration": cal, "heating_up": heat}


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    if len(args) == 2:
        run(list(range(args[0], args[1] + 1)))
    elif args:
        run(args)
    else:
        run()
