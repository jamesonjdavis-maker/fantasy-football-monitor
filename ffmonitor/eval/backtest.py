"""Leakage-safe historical backtest of the recommendation logic.

Replays the model's decision rules across completed NFL seasons and grades them
against what actually happened. Three backtests, matching the live tool's flags:

  1. start_sit  — when the model projects player A over a same-position player B
                  by the margin, how often did A actually outscore B, and by how
                  much? (validates the bench-over-starter / start-sit signal)
  2. waiver     — flag the best "waiver-pool" players (currently below their
                  position's replacement level) using data through week W, then
                  measure their production over weeks W+1..W+3 vs replacement.
  3. lineup     — how much of the week's *achievable* points did the model's
                  projection capture, vs a season-average baseline?

Method notes (for honesty on a resume):
  * "Projection" here is a reconstructed, leakage-safe form estimate — a trailing
    3-week PPG (weeks strictly before the graded week). The live tool uses ESPN's
    weekly projection, which isn't published for past seasons; this backtest
    therefore validates the *decision logic + a simple form model*, not ESPN's
    numbers. Always describe results as "in backtesting."
  * PPR scoring, regular season, skill positions (QB/RB/WR/TE).
  * Replacement levels use standard 12-team ranks (QB14/RB29/WR29/TE14), the same
    default the live tool falls back to.

Run:  python -m ffmonitor.eval.backtest            (last 6 completed seasons)
      python -m ffmonitor.eval.backtest 2018 2024  (an explicit range)
"""

from __future__ import annotations

import sys
from datetime import date

_NFLVERSE = ("https://github.com/nflverse/nflverse-data/releases/"
             "download/player_stats/player_stats_{yr}.parquet")
_POSITIONS = ("QB", "RB", "WR", "TE")
# Standard 12-team "last startable" rank per position (1QB/2RB/2WR/1TE/1FLEX).
_DEFAULT_RANKS = {"QB": 14, "RB": 29, "WR": 29, "TE": 14}
# Starters per position for the lineup-capture backtest (1QB/2RB/2WR/1TE).
_STARTERS_K = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
_POOL_SIZE = 30  # "startable pool" per position-week (keeps comparisons realistic)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def _completed_seasons(n: int = 6) -> list[int]:
    today = date.today()
    current = today.year if today.month >= 8 else today.year - 1
    return list(range(current - n, current))


def load_weekly(seasons: list[int]):
    """Read nflverse weekly player stats directly (no nfl_data_py), skipping any
    season not yet published."""
    import pandas as pd

    frames, used = [], []
    for yr in seasons:
        try:
            frames.append(pd.read_parquet(_NFLVERSE.format(yr=yr)))
            used.append(yr)
        except Exception as exc:  # noqa: BLE001 - resilient by design
            print(f"  season {yr} unavailable ({type(exc).__name__}); skipping")
    if not frames:
        raise RuntimeError("no seasons could be loaded")
    df = pd.concat(frames, ignore_index=True)
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["position"].isin(_POSITIONS)].copy()
    df["ppr"] = pd.to_numeric(df.get("fantasy_points_ppr", 0), errors="coerce").fillna(0.0)
    cols = ["player_id", "player_display_name", "position", "season", "week", "ppr"]
    print(f"  loaded seasons {used}: {len(df):,} player-weeks")
    return df[cols]


def prepare(df):
    """Add leakage-safe form features and forward outcomes.

      roll3 : mean PPR over the 3 weeks strictly BEFORE this week
      ttd   : mean PPR over ALL weeks strictly before this week (season-to-date)
      proj  : the model projection = recency-weighted season average
      lw    : last week's points (a naive point-chasing baseline)
      fut3  : mean PPR over the NEXT up-to-3 weeks (the waiver outcome window)
      fut_n : how many of those future weeks exist (>=1 to be gradable)
    """
    import pandas as pd

    df = df.sort_values(["player_id", "season", "week"]).reset_index(drop=True)
    g = df.groupby(["player_id", "season"])["ppr"]
    df["roll3"] = g.transform(lambda s: s.rolling(3, min_periods=1).mean().shift(1))
    df["ttd"] = g.transform(lambda s: s.expanding().mean().shift(1))
    df["lw"] = g.shift(1)  # last week's points — a naive point-chasing signal
    # Recency-weighted season average: the model's projection. Leans on the
    # stable season baseline but nudges toward recent form.
    df["proj"] = 0.3 * df["roll3"] + 0.7 * df["ttd"]

    fut = pd.DataFrame({i: g.shift(-i) for i in (1, 2, 3)})
    df["fut3"] = fut.mean(axis=1)
    df["fut_n"] = fut.notna().sum(axis=1)
    return df


def replacement_levels(df, ranks=None) -> dict:
    """Per-position replacement PPG: the rank-th best score each week, averaged."""
    ranks = ranks or _DEFAULT_RANKS
    levels = {}
    for pos, rank in ranks.items():
        pos_df = df[df["position"] == pos]

        def at_rank(s):
            import numpy as np
            arr = np.sort(s.to_numpy())[::-1]
            return float(arr[min(rank - 1, len(arr) - 1)]) if len(arr) else 0.0

        per_week = pos_df.groupby(["season", "week"])["ppr"].apply(at_rank)
        levels[pos] = round(float(per_week.mean()), 2) if len(per_week) else 0.0
    return levels


# --------------------------------------------------------------------------- #
# Backtest 1: start / sit accuracy (pairwise)
# --------------------------------------------------------------------------- #
def backtest_start_sit(df, margin: float = 2.0, pool_size: int = _POOL_SIZE) -> dict:
    """Among same-position startable pairs each week, when the model projects A
    over B by >= margin, how often did A actually outscore B, and by how much?"""
    d = df.dropna(subset=["proj"]).copy()
    # Keep only a realistic startable pool per position-week (top by projection).
    d = d[d["proj"] > 0]
    d["rk"] = d.groupby(["season", "week", "position"])["proj"].rank(
        ascending=False, method="first")
    d = d[d["rk"] <= pool_size]

    keys = ["season", "week", "position"]
    left = d[keys + ["player_id", "proj", "ppr"]]
    pairs = left.merge(left, on=keys, suffixes=("_a", "_b"))
    pairs = pairs[pairs["player_id_a"] != pairs["player_id_b"]]
    # A is the recommended start: higher projection by at least the margin.
    rec = pairs[pairs["proj_a"] - pairs["proj_b"] >= margin]
    if rec.empty:
        return {"n": 0}
    correct = (rec["ppr_a"] > rec["ppr_b"]).mean()
    gained = (rec["ppr_a"] - rec["ppr_b"]).mean()
    # Coin-flip baseline: without the model you'd be right ~50% on a random pick.
    return {
        "n": int(len(rec)),
        "accuracy": round(float(correct) * 100, 1),
        "avg_pts_gained": round(float(gained), 2),
        "margin": margin,
    }


# --------------------------------------------------------------------------- #
# Backtest 2: waiver value lift
# --------------------------------------------------------------------------- #
def backtest_waiver(df, repl: dict, top_n: int = 3) -> dict:
    """Flag the top waiver-pool players by value score (using data through W),
    then measure weeks W+1..W+3 vs their position's replacement level."""
    d = df.dropna(subset=["proj", "roll3", "ttd"]).copy()
    d = d[d["fut_n"] >= 1]  # gradable: at least one future week exists
    d["repl"] = d["position"].map(repl)
    # Waiver pool = currently BELOW replacement on the season (a realistic add).
    pool = d[d["ttd"] < d["repl"]].copy()
    if pool.empty:
        return {"n": 0}

    # Value score: over-replacement projection + a hot-hand bonus (leakage-safe),
    # mirroring the live tool's waiver value (VORP + recent form).
    hot = (pool["roll3"] - pool["ttd"]).clip(lower=0)
    pool["value"] = (pool["proj"] - pool["repl"]) + 0.5 * hot
    pool["rk"] = pool.groupby(["season", "week", "position"])["value"].rank(
        ascending=False, method="first")

    flagged = pool[pool["rk"] <= top_n]
    flag_hit = (flagged["fut3"] > flagged["repl"]).mean()
    flag_lift = (flagged["fut3"] - flagged["repl"]).mean()
    base_hit = (pool["fut3"] > pool["repl"]).mean()  # base rate across the pool
    return {
        "n": int(len(flagged)),
        "hit_rate": round(float(flag_hit) * 100, 1),
        "pool_base_rate": round(float(base_hit) * 100, 1),
        "avg_lift": round(float(flag_lift), 2),
        "top_n": top_n,
    }


# --------------------------------------------------------------------------- #
# Backtest 3: lineup capture rate
# --------------------------------------------------------------------------- #
def backtest_lineup(df, pool_size: int = _POOL_SIZE) -> dict:
    """For each position-week, over a startable pool, compare the actual points of
    the top-K by model projection against two baselines — season-to-date average
    (a strong baseline) and chasing last week's points (a naive one) — each as a
    share of the best-possible (hindsight-optimal) top-K."""
    d = df.dropna(subset=["proj", "ttd"]).copy()
    d = d[d["proj"] > 0]

    model_pts = savg_pts = chase_pts = max_pts = 0.0
    for (_s, _w, pos), grp in d.groupby(["season", "week", "position"]):
        k = _STARTERS_K.get(pos, 1)
        pool = grp.sort_values("proj", ascending=False).head(pool_size)
        if len(pool) < k:
            continue
        best = pool.sort_values("ppr", ascending=False).head(k)["ppr"].sum()
        if best <= 0:
            continue
        model_pts += pool.sort_values("proj", ascending=False).head(k)["ppr"].sum()
        savg_pts += pool.sort_values("ttd", ascending=False).head(k)["ppr"].sum()
        chase = pool.dropna(subset=["lw"]).sort_values("lw", ascending=False)
        chase_pts += chase.head(k)["ppr"].sum()
        max_pts += best
    if max_pts <= 0:
        return {"n": 0}
    return {
        "model_capture": round(model_pts / max_pts * 100, 1),
        "seasonavg_capture": round(savg_pts / max_pts * 100, 1),
        "chase_capture": round(chase_pts / max_pts * 100, 1),
    }


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(seasons: list[int] | None = None) -> dict:
    seasons = seasons or _completed_seasons(6)
    print(f"Backtesting seasons {seasons[0]}–{seasons[-1]} …")
    df = prepare(load_weekly(seasons))
    repl = replacement_levels(df)
    print(f"  replacement PPG (12-team): {repl}")

    ss = backtest_start_sit(df)
    wv = backtest_waiver(df, repl)
    lu = backtest_lineup(df)

    print("\n=== RESULTS ===")
    if ss.get("n"):
        print(f"[start/sit] {ss['n']:,} graded calls | correct {ss['accuracy']}% "
              f"(vs ~50% coin flip) | avg +{ss['avg_pts_gained']} pts to the "
              f"favored start (margin ≥ {ss['margin']})")
    if wv.get("n"):
        ratio = wv["hit_rate"] / wv["pool_base_rate"] if wv["pool_base_rate"] else 0
        print(f"[waiver]    {wv['n']:,} flagged | became startable (beat "
              f"replacement) next 3 wks {wv['hit_rate']}% vs {wv['pool_base_rate']}% "
              f"pool base rate — {ratio:.1f}x lift")
    if lu.get("model_capture") is not None:
        print(f"[lineup]    captured {lu['model_capture']}% of optimal points "
              f"| season-avg {lu['seasonavg_capture']}% | "
              f"chase-last-week {lu['chase_capture']}%")

    return {"seasons": seasons, "replacement": repl,
            "start_sit": ss, "waiver": wv, "lineup": lu}


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    if len(args) == 2:
        run(list(range(args[0], args[1] + 1)))
    elif args:
        run(args)
    else:
        run()
