"""Bridge between the ML module and the monitor.

Exposes the rising-usage signal to analyze.py in a way that can NEVER break the
core monitor: if the ML dependencies aren't installed, or the nfl_data_py pull
fails (nflverse down, network hiccup), this returns an empty dict and the daily
run proceeds without breakout flags.
"""

from __future__ import annotations


def attach_projection_ranges(snapshot: dict) -> None:
    """Add Monte Carlo floor/median/ceiling to each roster player, in place.
    Safe no-op if numpy/pandas or history data are unavailable."""
    try:
        from .montecarlo import attach_ranges
    except Exception as exc:  # pragma: no cover
        print(f"[ml] montecarlo module unavailable: {exc}")
        return
    try:
        attach_ranges(snapshot)
        print("[ml] Monte Carlo floor/ceiling attached to rosters.")
    except Exception as exc:
        print(f"[ml] Monte Carlo unavailable this run: {exc}")


def get_replacement_levels() -> dict | None:
    """Per-position replacement level (weekly PPG) so waiver value can be scored
    over replacement, making positions comparable. None if unavailable."""
    try:
        from .history import replacement_levels
    except Exception as exc:  # pragma: no cover
        print(f"[ml] replacement-levels module unavailable: {exc}")
        return None
    try:
        lv = replacement_levels()
        print(f"[ml] replacement levels: {lv}")
        return lv
    except Exception as exc:
        print(f"[ml] replacement levels unavailable this run: {exc}")
        return None


def get_production_thresholds() -> dict | None:
    """Historical per-position 'notably rising' thresholds (recent PPG vs season
    average), calibrated from many completed seasons. Returns None if the
    history pull fails, so the monitor runs without breakout flags."""
    try:
        from .history import fantasy_jump_thresholds
    except Exception as exc:  # pragma: no cover
        print(f"[ml] history module unavailable: {exc}")
        return None
    try:
        cal = fantasy_jump_thresholds()
        print(
            f"[ml] production thresholds from seasons "
            f"{sorted(cal['seasons'])}: {cal['thresholds']}"
        )
        return cal
    except Exception as exc:
        print(f"[ml] production thresholds unavailable this run: {exc}")
        return None


def get_rising_scores(seasons: list[int] | None = None) -> dict[str, dict]:
    """Return {normalized_name: {score, usage_delta, opp_share, rising, ...}}
    for the latest NFL week, or {} if the signal is unavailable this run."""
    try:
        from .baseline import _default_seasons, rising_usage_scores
    except Exception as exc:  # pragma: no cover - import guard
        print(f"[ml] signal module unavailable: {exc}")
        return {}

    try:
        scores = rising_usage_scores(seasons or _default_seasons())
        print(f"[ml] rising-usage signal loaded for {len(scores)} players.")
        return scores
    except Exception as exc:
        # pandas/nfl_data_py missing, or the data pull failed. Degrade quietly.
        print(f"[ml] rising-usage signal unavailable this run: {exc}")
        return {}
