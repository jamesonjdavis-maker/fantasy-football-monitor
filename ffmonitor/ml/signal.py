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


def attach_replacement_levels(snapshot: dict) -> None:
    """Attach per-league replacement levels to each snap, derived from that
    league's detected settings (team count + starting lineup). Falls back to the
    12-team default when settings are missing. Safe no-op on failure."""
    try:
        from .history import ranks_from_settings, replacement_levels
    except Exception as exc:  # pragma: no cover
        print(f"[ml] replacement-levels module unavailable: {exc}")
        return
    for key, snap in snapshot.get("platforms", {}).items():
        if not isinstance(snap, dict) or not snap.get("enabled"):
            continue
        try:
            ls = snap.get("league_settings")
            ranks = (
                ranks_from_settings(ls["team_count"], ls["starters"]) if ls else None
            )
            snap["replacement_levels"] = replacement_levels(ranks)
            print(
                f"[ml] {key}: replacement levels {snap['replacement_levels']}"
                + (f" (ranks {ranks})" if ranks else " (12-team default)")
            )
        except Exception as exc:
            print(f"[ml] {key}: replacement levels unavailable ({exc})")


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
