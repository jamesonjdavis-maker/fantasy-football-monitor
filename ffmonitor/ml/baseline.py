"""Rule-based 'rising usage' signal — the baseline any ML model must beat.

The rule: a player whose opportunity share this week jumped meaningfully above
their trailing 3-week baseline, off a non-trivial workload, is a buy-low /
breakout candidate. No training, no labels — just the usage trend from data.py.

Run it directly to eyeball the current week's risers:
    python -m ffmonitor.ml.baseline
    python -m ffmonitor.ml.baseline --seasons 2024 2025 --top 30
"""

from __future__ import annotations

import argparse
import re
import unicodedata

from . import DEFAULT_POSITIONS
from .data import build_usage_frame, latest_season_week

# Rule thresholds (tune against your leagues before trusting them).
MIN_USAGE_DELTA = 0.08   # share must rise ≥ 8 percentage points vs baseline
MIN_OPP_SHARE = 0.10     # ...off at least a 10% opportunity share this week
SCORE_FULL_AT = 0.20     # a +20pt jump maps to a score of 1.0


def _score(delta: float) -> float:
    """Map a usage_delta into a 0–1 'rising' score (clipped linear ramp)."""
    if delta is None or delta != delta:  # None or NaN
        return 0.0
    return max(0.0, min(delta / SCORE_FULL_AT, 1.0))


def normalize_name(name: str) -> str:
    """Canonicalize a player name so nfl_data_py names line up with the
    ESPN/Sleeper snapshot names (strip accents, punctuation, Jr/Sr/II/III,
    lowercase). Used later to attach scores to your roster/free agents."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    name = re.sub(r"[.\-']", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def rising_usage_table(
    seasons, positions=DEFAULT_POSITIONS, season=None, week=None
):
    """Ranked DataFrame of risers for one week (default: latest available)."""
    frame = build_usage_frame(seasons, positions)
    if season is None or week is None:
        season, week = latest_season_week(frame)

    wk = frame[(frame["season"] == season) & (frame["week"] == week)].copy()
    wk["score"] = wk["usage_delta"].apply(_score)
    wk["rising"] = (
        (wk["usage_delta"] >= MIN_USAGE_DELTA)
        & (wk["opp_share"] >= MIN_OPP_SHARE)
        & wk["usage_trailing3"].notna()
    )
    wk["name_key"] = wk["player"].apply(normalize_name)
    wk = wk.sort_values(["score", "opp_share"], ascending=False)
    return wk, season, week


def rising_usage_scores(seasons, positions=DEFAULT_POSITIONS) -> dict[str, dict]:
    """Map normalized-name -> {score, delta, opp_share, ...} for the latest
    week. This is the hook analyze.py will call to enrich snapshot players."""
    wk, _, _ = rising_usage_table(seasons, positions)
    out: dict[str, dict] = {}
    for row in wk.itertuples(index=False):
        out[row.name_key] = {
            "score": round(float(row.score), 3),
            "usage_delta": round(float(row.usage_delta), 3),
            "opp_share": round(float(row.opp_share), 3),
            "rising": bool(row.rising),
            "position": row.position,
            "team": row.team,
        }
    return out


def _default_seasons() -> list[int]:
    """Baseline only needs the current season; use it, with last season as a
    fallback so early-season weeks (thin trailing data) still return rows."""
    from datetime import date

    y = date.today().year
    season = y if date.today().month >= 8 else y - 1
    return [season - 1, season]


def main() -> None:
    parser = argparse.ArgumentParser(description="Print this week's usage risers.")
    parser.add_argument("--seasons", type=int, nargs="+", default=_default_seasons())
    parser.add_argument("--positions", nargs="+", default=list(DEFAULT_POSITIONS))
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--flagged-only", action="store_true",
                        help="show only players the rule flags as rising")
    args = parser.parse_args()

    wk, season, week = rising_usage_table(args.seasons, args.positions)
    if args.flagged_only:
        wk = wk[wk["rising"]]
    wk = wk.head(args.top)

    print(f"\nRising-usage risers — {season} Week {week} "
          f"({', '.join(args.positions)})\n")
    header = f"{'Player':<24}{'Pos':<5}{'Tm':<5}{'Share':>7}{'Base':>7}{'Δ':>7}{'Score':>7}  Flag"
    print(header)
    print("-" * len(header))
    for r in wk.itertuples(index=False):
        flag = "★" if r.rising else ""
        base = "  n/a" if r.usage_trailing3 != r.usage_trailing3 else f"{r.usage_trailing3:6.2f}"
        print(
            f"{r.player[:23]:<24}{r.position:<5}{r.team:<5}"
            f"{r.opp_share:7.2f}{base:>7}{r.usage_delta:7.2f}{r.score:7.2f}  {flag}"
        )
    print(f"\n★ = flagged rising (Δ ≥ {MIN_USAGE_DELTA:.0%}, "
          f"share ≥ {MIN_OPP_SHARE:.0%}). Tune thresholds in baseline.py.\n")


if __name__ == "__main__":
    main()
