"""Bridge between the ML module and the monitor.

Exposes the rising-usage signal to analyze.py in a way that can NEVER break the
core monitor: if the ML dependencies aren't installed, or the nfl_data_py pull
fails (nflverse down, network hiccup), this returns an empty dict and the daily
run proceeds without breakout flags.
"""

from __future__ import annotations


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
