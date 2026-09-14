"""Point-in-time analysis of the current snapshot.

Unlike diff.py (which needs yesterday), these flags are computed from today's
snapshot alone:
  * bench players projected to outscore a same-position starter this week
  * "weak" starting slots (low projection or an injured/bye starter) paired
    with trending free agents that could fill them
"""

from __future__ import annotations

from typing import Any

from .config import Thresholds

# Positions a FLEX starter can be replaced from, for weak-position matching.
_FLEX_POSITIONS = {"RB", "WR", "TE"}
_UNAVAILABLE = {"OUT", "IR", "DOUBTFUL", "SUSPENDED", "PUP"}


def _proj(player: dict) -> float | None:
    return player.get("proj_points")


def _starters(roster: list[dict]) -> list[dict]:
    return [p for p in roster if p.get("slot") == "starter"]


def _bench(roster: list[dict]) -> list[dict]:
    return [p for p in roster if p.get("slot") == "bench"]


def _bench_beats_starter(
    platform: str, snap: dict, thresholds: Thresholds
) -> list[dict]:
    """Flag bench players projected to beat a same-position starter."""
    flags: list[dict] = []
    roster = snap.get("roster", [])
    starters = _starters(roster)
    bench = _bench(roster)

    for b in bench:
        bproj = _proj(b)
        if bproj is None:
            continue
        pos = b.get("position")
        # Compare against starters at the same position (or FLEX-eligible).
        candidates = [
            s
            for s in starters
            if s.get("position") == pos
            or (pos in _FLEX_POSITIONS and s.get("position") in _FLEX_POSITIONS)
        ]
        for s in candidates:
            sproj = _proj(s)
            if sproj is None:
                continue
            starter_hurt = s.get("injury_status") in _UNAVAILABLE or s.get("on_bye")
            margin = bproj - sproj
            if margin >= thresholds.bench_over_starter_margin or starter_hurt:
                reason = (
                    "starter is OUT/bye"
                    if starter_hurt
                    else f"+{margin:.1f} proj pts"
                )
                flags.append(
                    {
                        "platform": platform,
                        "kind": "bench_over_starter",
                        "severity": "high" if starter_hurt else "medium",
                        "message": (
                            f"Start {b['name']} ({pos}, {bproj:.1f}) over "
                            f"{s['name']} ({s.get('position')}, "
                            f"{sproj if sproj is not None else '?'}) — {reason}"
                        ),
                        "bench_player": b,
                        "starter": s,
                    }
                )
                break  # one suggestion per bench player is enough
    return flags


def _weak_positions(snap: dict, thresholds: Thresholds) -> set[str]:
    """Positions where a starter is weak: low projection, injured, or on bye."""
    weak: set[str] = set()
    for s in _starters(snap.get("roster", [])):
        pos = s.get("position")
        if not pos:
            continue
        proj = _proj(s)
        if (
            (proj is not None and proj < thresholds.weak_starter_proj)
            or s.get("injury_status") in _UNAVAILABLE
            or s.get("on_bye")
        ):
            weak.add(pos)
    return weak


def _trending_at_weak_positions(
    platform: str, snap: dict, thresholds: Thresholds
) -> list[dict]:
    weak = _weak_positions(snap, thresholds)
    if not weak:
        return []
    flags: list[dict] = []
    for fa in snap.get("free_agents", []):
        pos = fa.get("position")
        if pos not in weak:
            continue
        count = fa.get("percent_owned") or 0
        # On Sleeper `percent_owned` carries the trending add count; on ESPN it
        # is a genuine ownership percentage. Treat both as "interest".
        hot = count >= thresholds.hot_add_min_count or (
            0 < count <= 100 and count >= thresholds.espn_hot_owned_pct
        )
        if not hot:
            continue
        flags.append(
            {
                "platform": platform,
                "kind": "weak_position_fa",
                "severity": "medium",
                "message": (
                    f"Weak at {pos}: consider {fa['name']} "
                    f"({fa.get('pro_team')}) — trending/owned {int(count):,}"
                ),
                "player": fa,
                "position": pos,
            }
        )
    return flags


def _rising_production_flags(
    platform: str, snap: dict, calibration: dict | None
) -> list[dict]:
    """ML signal: flag players whose *recent* fantasy form (last few weeks, from
    ESPN) is running above their own season average by more than the
    historically-calibrated, position-specific bar (from ml.history). Covers
    bench players (start candidates) and free agents (buy-low adds)."""
    if not calibration:
        return []
    thresholds = calibration.get("thresholds", {})
    flags: list[dict] = []

    candidates = [(p, "ml_rising_bench") for p in snap.get("roster", [])
                  if p.get("slot") == "bench"]
    candidates += [(fa, "ml_rising_fa") for fa in snap.get("free_agents", [])]

    for p, kind in candidates:
        recent = p.get("recent_points") or []
        baseline = p.get("avg_points")
        pos = p.get("position")
        bar = thresholds.get(pos)
        if len(recent) < 2 or baseline is None or bar is None:
            continue
        recent_ppg = sum(recent) / len(recent)
        delta = recent_ppg - baseline
        if delta >= bar and recent_ppg >= 5.0:
            where = "bench" if kind == "ml_rising_bench" else "FA"
            flags.append(
                {
                    "platform": platform,
                    "kind": kind,
                    "severity": "medium",
                    "message": (
                        f"Heating up ({where}): {p['name']} ({pos}) — "
                        f"{recent_ppg:.1f} PPG last {len(recent)} wks vs "
                        f"{baseline:.1f} season avg (+{delta:.1f})"
                    ),
                    "player": p,
                }
            )
    return flags


def analyze(
    snapshot: dict,
    thresholds: Thresholds,
    production_thresholds: dict | None = None,
) -> list[dict]:
    """Return all point-in-time flags across platforms, most severe first.

    `production_thresholds` (optional, from ml.history) enables the ML
    "heating up" flags; when omitted, only the rule-based start/sit and waiver
    flags run.
    """
    from .diff import SEVERITY_ORDER

    flags: list[dict] = []
    for platform, snap in snapshot.get("platforms", {}).items():
        if not snap or snap.get("error") or not snap.get("enabled"):
            continue
        flags.extend(_bench_beats_starter(platform, snap, thresholds))
        flags.extend(_trending_at_weak_positions(platform, snap, thresholds))
        flags.extend(_rising_production_flags(platform, snap, production_thresholds))

    flags.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 0), reverse=True)
    return flags
