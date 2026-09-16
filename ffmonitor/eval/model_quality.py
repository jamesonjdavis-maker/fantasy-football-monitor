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
def _tier_col(proj):
    """Vectorized tier from projection: low<9, mid<14, else high."""
    import numpy as np
    return np.where(proj < 9, "low", np.where(proj < 14, "mid", "high"))


def _pct_table(train_df, q: float):
    """Per (position, tier) q / (100-q) percentiles of the performance multiplier,
    plus a pooled (position, 'all') fallback — as a tidy DataFrame for merging."""
    import numpy as np
    import pandas as pd

    m = train_df.copy()
    m["savg"] = m.groupby(["player_id", "season"])["ppr"].transform("mean")
    m = m[m["savg"] >= _MIN_SEASON_AVG]
    m["mult"] = m["ppr"] / m["savg"]
    m["tier"] = _tier_col(m["savg"])

    rows = []
    for pos in _POSITIONS:
        pos_m = m[m["position"] == pos]
        allv = pos_m["mult"].to_numpy()
        if len(allv):
            rows.append((pos, "all", np.percentile(allv, q), np.percentile(allv, 100 - q)))
        for tier in ("low", "mid", "high"):
            arr = pos_m.loc[pos_m["tier"] == tier, "mult"].to_numpy()
            if len(arr) >= 200:
                rows.append((pos, tier, np.percentile(arr, q), np.percentile(arr, 100 - q)))
    return pd.DataFrame(rows, columns=["position", "tier", "p_lo", "p_hi"])


def _coverage(eval_df, pct_table) -> dict:
    """Vectorized coverage of proj × [p_lo, p_hi] over the eval rows."""
    import pandas as pd

    d = eval_df.dropna(subset=["proj"]).copy()
    d = d[d["proj"] >= _MIN_CONTRIB]
    if d.empty:
        return {"n": 0}
    d["tier"] = _tier_col(d["proj"])
    # Prefer the tier-specific band; fall back to the position's pooled band.
    tier_tbl = pct_table[pct_table["tier"] != "all"]
    all_tbl = pct_table[pct_table["tier"] == "all"].drop(columns="tier")
    d = d.merge(tier_tbl, on=["position", "tier"], how="left")
    d = d.merge(all_tbl, on="position", how="left", suffixes=("", "_all"))
    d["p_lo"] = d["p_lo"].fillna(d["p_lo_all"])
    d["p_hi"] = d["p_hi"].fillna(d["p_hi_all"])
    d = d.dropna(subset=["p_lo", "p_hi"])
    if d.empty:
        return {"n": 0}

    floor = d["proj"] * d["p_lo"]
    ceiling = d["proj"] * d["p_hi"]
    inside = ((d["ppr"] >= floor) & (d["ppr"] <= ceiling))
    n = len(d)
    return {
        "n": int(n),
        "coverage_pct": round(float(inside.mean()) * 100, 1),
        "below_floor_pct": round(float((d["ppr"] < floor).mean()) * 100, 1),
        "above_ceiling_pct": round(float((d["ppr"] > ceiling).mean()) * 100, 1),
        "avg_band_pts": round(float((ceiling - floor).mean()), 1),
    }


def calibration(df, q: float = 10.0) -> dict:
    """Coverage of the raw multiplier band (default P10–P90) — in-sample."""
    return _coverage(df, _pct_table(df, q))


def calibrate(df, target: float = 80.0) -> dict:
    """Widen the band to hit the target coverage, fitting the tail percentile on
    all-but-the-last season and reporting coverage on the held-out last season —
    so 'calibrated to ~80%' is a genuine out-of-sample result."""
    seasons = sorted(df["season"].unique())
    if len(seasons) < 2:
        return {"n": 0}
    train = df[df["season"].isin(seasons[:-1])]
    test = df[df["season"] == seasons[-1]]

    raw = _coverage(test, _pct_table(train, 10.0))  # before: fixed P10–P90
    # Lower q ⇒ wider band ⇒ higher coverage. Scan down until train hits target.
    best_q = 10.0
    for q in [x / 2 for x in range(20, 1, -1)]:  # 10.0, 9.5, … 1.0
        if _coverage(train, _pct_table(train, q)).get("coverage_pct", 0) >= target:
            best_q = q
            break
    calibrated = _coverage(test, _pct_table(train, best_q))
    return {
        "tail_percentile": best_q,
        "raw": raw,
        "calibrated": calibrated,
        "test_season": int(seasons[-1]),
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
    cal = calibrate(df)
    heat = heating_up_precision(df)

    print("\n=== RESULTS ===")
    if mae.get("n"):
        print(f"[projection] MAE {mae['mae_model']} pts | season-avg {mae['mae_seasonavg']} "
              f"| last-week {mae['mae_lastweek']}  → {mae['vs_seasonavg_pct']}% better than "
              f"season-avg, {mae['vs_lastweek_pct']}% better than last-week ({mae['n']:,} wks)")
    if cal.get("calibrated", {}).get("n"):
        raw, c = cal["raw"], cal["calibrated"]
        print(f"[calibration] held-out {cal['test_season']}: raw P10–P90 band covered "
              f"{raw['coverage_pct']}% of outcomes → widened to P{cal['tail_percentile']:g} "
              f"covers {c['coverage_pct']}% (target 80) | avg band {c['avg_band_pts']} pts "
              f"({c['n']:,} predictions)")
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
