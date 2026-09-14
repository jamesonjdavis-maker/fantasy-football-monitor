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


def _range_note(bench: dict, starter: dict) -> str:
    """Append Monte Carlo floor–ceiling ranges to a start/sit line when
    available, so the swap's risk profile is visible."""
    if bench.get("proj_floor") is None or starter.get("proj_floor") is None:
        return ""
    return (
        f" | ranges {bench['name'].split()[-1]} {bench['proj_floor']}–"
        f"{bench['proj_ceiling']} vs {starter['name'].split()[-1]} "
        f"{starter['proj_floor']}–{starter['proj_ceiling']}"
    )


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
                            + _range_note(b, s)
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


_SCARCITY_BONUS = 3.0  # points added when an FA fills a position you're weak at


def _waiver_targets(
    platform: str,
    snap: dict,
    thresholds: Thresholds,
    top_n: int = 3,
) -> list[dict]:
    """Rank available free agents into the best pickups for THIS roster. The
    value score is **over replacement** so positions compare fairly:
      - base: projection minus the position's replacement level (a freely
        available player at that spot) — a 9-pt TE beats a 9-pt WR
      - upside: credit for Monte Carlo ceiling above the projection (if ML on)
      - hot form: credit when recent PPG beats their season average
      - scarcity: a flat bonus if the FA fills a position you're weak at
    Emits the top N as ranked 'waiver_target' flags."""
    fas = snap.get("free_agents", [])
    if not fas:
        return []
    weak = _weak_positions(snap, thresholds)
    repl = snap.get("replacement_levels") or {}  # league-specific (per snap)

    scored: list[tuple[float, dict, float, list[str]]] = []
    for fa in fas:
        proj = _proj(fa) or fa.get("avg_points")
        if proj is None or proj < 4.0:  # ignore roster-filler noise
            continue
        pos = fa.get("position")
        reasons: list[str] = []

        # Value over replacement: points above a freely-available player at this
        # position. Falls back to raw projection when no baseline is available.
        baseline = repl.get(pos)
        value = float(proj) - baseline if baseline is not None else float(proj)

        ceiling = fa.get("proj_ceiling")
        if ceiling is not None:
            value += 0.3 * max(0.0, ceiling - proj)
            reasons.append(f"ceiling {ceiling}")

        recent = fa.get("recent_points") or []
        avg = fa.get("avg_points")
        if len(recent) >= 2 and avg is not None:
            recent_ppg = sum(recent) / len(recent)
            if recent_ppg > avg:
                value += (recent_ppg - avg) * 0.5
                reasons.append("hot form")

        if pos in weak:
            value += _SCARCITY_BONUS  # additive so it always raises priority
            reasons.append(f"weak at {pos}")

        owned = fa.get("percent_owned") or 0
        if owned and owned > 100:  # Sleeper trending-add count
            reasons.append(f"{int(owned):,} adds")

        scored.append((value, fa, float(proj), reasons))

    scored.sort(key=lambda t: t[0], reverse=True)
    flags: list[dict] = []
    for value, fa, proj, reasons in scored[:top_n]:
        detail = f" — {', '.join(reasons)}" if reasons else ""
        flags.append(
            {
                "platform": platform,
                # Uniform severity so the global sort preserves the value ranking
                # within this section (the score already weights scarcity).
                "kind": "waiver_target",
                "severity": "medium",
                "message": (
                    f"{fa['name']} ({fa.get('position')}, {fa.get('pro_team')}) "
                    f"— value {value:.1f} (proj {proj:.1f}){detail}"
                ),
                "player": fa,
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


def _projection_range_flags(platform: str, snap: dict) -> list[dict]:
    """Monte Carlo floor/ceiling insight (only fires when ranges are attached):
    surface a bench player who, despite an equal-or-lower median projection than
    a same-position starter, offers a notably higher CEILING (upside dart) or
    higher FLOOR (safer play) — the nuance a single projection can't show."""
    CEIL_MARGIN = 3.0
    FLOOR_MARGIN = 2.0

    def _ranged(players: list[dict]) -> list[dict]:
        return [p for p in players if p.get("proj_ceiling") is not None]

    starters = _ranged([p for p in snap.get("roster", []) if p.get("slot") == "starter"])
    bench = _ranged([p for p in snap.get("roster", []) if p.get("slot") == "bench"])
    flags: list[dict] = []

    for b in bench:
        pos = b.get("position")
        peers = [s for s in starters if s.get("position") == pos]
        for s in peers:
            # Only add insight the point projection doesn't already give: the
            # bench player's median is not higher, yet a tail is.
            if b["proj_median"] > s["proj_median"]:
                continue
            if b["proj_ceiling"] >= s["proj_ceiling"] + CEIL_MARGIN:
                flags.append({
                    "platform": platform, "kind": "mc_upside", "severity": "low",
                    "message": (
                        f"Upside dart: {b['name']} ({pos}) ceiling "
                        f"{b['proj_ceiling']} vs {s['name']} ceiling "
                        f"{s['proj_ceiling']} — more boom if you need points"
                    ),
                    "bench_player": b, "starter": s,
                })
                break
            if b["proj_floor"] >= s["proj_floor"] + FLOOR_MARGIN:
                flags.append({
                    "platform": platform, "kind": "mc_floor", "severity": "low",
                    "message": (
                        f"Safer floor: {b['name']} ({pos}) floor "
                        f"{b['proj_floor']} vs {s['name']} floor "
                        f"{s['proj_floor']} — steadier if you're protecting a lead"
                    ),
                    "bench_player": b, "starter": s,
                })
                break
    return flags


def _game_script_flags(
    platform: str, snap: dict, spreads: dict[str, float], threshold: float
) -> list[dict]:
    """Vegas game-script signal from point spreads: a heavy favorite runs more
    (RB-friendly); a heavy underdog throws to catch up (RBs fade, pass-catchers
    get garbage-time volume). Flags your rostered skill players on lopsided
    games."""
    if not spreads:
        return []

    def _flag(p: dict, note: str, sev: str) -> dict:
        return {
            "platform": platform,
            "kind": "game_script",
            "severity": sev,
            "message": (
                f"{p['name']} ({p.get('position')}, {p.get('pro_team')}) — {note}"
            ),
            "player": p,
        }

    flags: list[dict] = []
    for p in snap.get("roster", []):
        if p.get("slot") not in ("starter", "bench"):
            continue
        pos = p.get("position")
        team = p.get("pro_team")
        if pos not in ("RB", "WR", "TE") or not team:
            continue
        spread = spreads.get(team)
        if spread is None:
            continue
        if spread <= -threshold and pos == "RB":
            flags.append(_flag(
                p, f"team favored by {abs(spread):.1f} — positive script (run-heavy)",
                "medium"))
        elif spread >= threshold:
            if pos == "RB":
                flags.append(_flag(
                    p, f"team underdog by {spread:.1f} — negative script, carries may thin",
                    "medium"))
            else:  # WR / TE
                flags.append(_flag(
                    p, f"team underdog by {spread:.1f} — garbage-time target upside",
                    "low"))
    return flags


def analyze(
    snapshot: dict,
    thresholds: Thresholds,
    production_thresholds: dict | None = None,
    spreads: dict[str, float] | None = None,
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
        flags.extend(_waiver_targets(platform, snap, thresholds))
        flags.extend(_rising_production_flags(platform, snap, production_thresholds))
        flags.extend(_projection_range_flags(platform, snap))
        flags.extend(
            _game_script_flags(platform, snap, spreads or {}, thresholds.game_script_spread)
        )

    flags.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 0), reverse=True)
    return flags
