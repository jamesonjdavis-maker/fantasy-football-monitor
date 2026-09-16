"""Live outcome logging — a running, real-world track record of the tool's calls.

Each run records the recommendations it makes, and a later run grades the ones
whose result is now known. This turns the tool from "backtested" into "and here
is what it actually got right this season."

Gradable today: **start/sit** calls (bench-over-starter). Both players are on
your roster, so their actual weekly points land in the daily snapshots we already
commit — a call for week W is graded from the latest stored snapshot still on
week W (which holds that week's final scores). A call is "correct" when the
player we said to start outscored the one we said to sit.

Other recommendation kinds (waiver targets, heating-up, game-script) are logged
for the record but not auto-graded — their outcomes aren't fully observable from
roster snapshots alone.

Everything here is fail-safe: logging or grading never breaks the monitor.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_LOG_NAME = "recommendations.json"
_GRADABLE_KINDS = {"bench_over_starter"}


def _log_path(data_dir: Path) -> Path:
    return data_dir / _LOG_NAME


def _load(data_dir: Path) -> list[dict]:
    path = _log_path(data_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data_dir: Path, entries: list[dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _log_path(data_dir).write_text(json.dumps(entries, indent=2, sort_keys=True))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _season(snapshot: dict) -> int:
    """NFL season year from the snapshot's timestamp (Aug+ = that year)."""
    stamp = snapshot.get("generated_at")
    try:
        d = datetime.fromisoformat(stamp).date()
    except (TypeError, ValueError):
        d = date.today()
    return d.year if d.month >= 8 else d.year - 1


def _week_of(snapshot: dict, league: str) -> int | None:
    plat = snapshot.get("platforms", {}).get(league)
    return plat.get("week") if isinstance(plat, dict) else None


def _slim(player: dict | None) -> dict:
    if not isinstance(player, dict):
        return {}
    return {
        "id": str(player.get("id")),
        "name": player.get("name"),
        "position": player.get("position"),
        "proj": player.get("proj_points"),
    }


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #
def record(data_dir: Path, snapshot: dict, flags: list[dict]) -> int:
    """Append any newly-made recommendations to the log (deduped). Returns the
    number of new entries added."""
    entries = _load(data_dir)
    seen = {e.get("id") for e in entries}
    season = _season(snapshot)
    added = 0

    for f in flags:
        kind = f.get("kind")
        league = f.get("platform")
        week = _week_of(snapshot, league)
        if week is None:
            continue

        if kind in _GRADABLE_KINDS:
            primary = _slim(f.get("bench_player"))   # recommended to START
            compare = _slim(f.get("starter"))        # recommended to SIT
            rec_id = f"{season}:{week}:{league}:{kind}:{primary.get('id')}:{compare.get('id')}"
            gradable = True
        else:
            primary = compare = {}
            rec_id = f"{season}:{week}:{league}:{kind}:{hash(f.get('message')) & 0xffffff}"
            gradable = False

        if rec_id in seen:
            continue
        seen.add(rec_id)
        entries.append({
            "id": rec_id,
            "logged_at": _now(),
            "season": season,
            "week": week,
            "league": league,
            "league_name": (snapshot.get("platforms", {}).get(league, {}) or {})
                .get("league_name"),
            "kind": kind,
            "message": f.get("message"),
            "primary": primary,
            "compare": compare,
            "gradable": gradable,
            "status": "open" if gradable else "logged",
            "graded_at": None,
            "primary_actual": None,
            "compare_actual": None,
            "points_delta": None,
        })
        added += 1

    if added:
        _save(data_dir, entries)
    return added


# --------------------------------------------------------------------------- #
# Grade
# --------------------------------------------------------------------------- #
def _actual_points(data_dir: Path, league: str, week: int, player_id: str) -> float | None:
    """Final points a player scored in `week`, read from the latest stored
    snapshot still on that week (newest-first so we get the finalized score)."""
    paths = sorted(data_dir.glob("snapshot-*.json"), reverse=True)
    for path in paths:
        try:
            snap = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        plat = snap.get("platforms", {}).get(league)
        if not isinstance(plat, dict) or plat.get("week") != week:
            continue
        for p in plat.get("roster", []):
            if str(p.get("id")) == str(player_id) and p.get("actual_points") is not None:
                return float(p["actual_points"])
    return None


def grade(data_dir: Path, snapshot: dict) -> int:
    """Grade open start/sit calls whose week is complete. Returns count graded."""
    entries = _load(data_dir)
    graded = 0
    changed = False

    for e in entries:
        if e.get("status") != "open" or not e.get("gradable"):
            continue
        league, week = e.get("league"), e.get("week")
        current_week = _week_of(snapshot, league)
        if current_week is None or week is None or week >= current_week:
            continue  # that week isn't finished yet

        pa = _actual_points(data_dir, league, week, e["primary"].get("id"))
        ca = _actual_points(data_dir, league, week, e["compare"].get("id"))
        if pa is None or ca is None:
            # Give up only once we're well past the week with no data on file.
            if week < current_week - 1:
                e["status"] = "skipped"
                e["graded_at"] = _now()
                changed = True
            continue

        e["primary_actual"] = round(pa, 2)
        e["compare_actual"] = round(ca, 2)
        e["points_delta"] = round(pa - ca, 2)
        e["status"] = "correct" if pa > ca else "incorrect"
        e["graded_at"] = _now()
        graded += 1
        changed = True

    if changed:
        _save(data_dir, entries)
    return graded


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def summary(data_dir: Path, season: int | None = None) -> dict:
    """Aggregate the graded start/sit record for the (current) season."""
    entries = _load(data_dir)
    if season is None and entries:
        season = max((e.get("season", 0) for e in entries), default=None)

    graded = [
        e for e in entries
        if e.get("kind") == "bench_over_starter"
        and e.get("status") in ("correct", "incorrect")
        and (season is None or e.get("season") == season)
    ]
    if not graded:
        return {}

    correct = sum(1 for e in graded if e["status"] == "correct")
    n = len(graded)
    deltas = [e["points_delta"] for e in graded if e.get("points_delta") is not None]
    avg_pts = round(sum(deltas) / len(deltas), 2) if deltas else 0.0
    return {
        "season": season,
        "start_sit": {
            "graded": n,
            "correct": correct,
            "accuracy": round(correct / n * 100, 1),
            "avg_pts_gained": avg_pts,
        },
    }


def summary_line(track: dict) -> str | None:
    """A one-liner for the alert footer, or None if there's no record yet."""
    ss = (track or {}).get("start_sit")
    if not ss or not ss.get("graded"):
        return None
    return (f"Start/sit record: {ss['correct']}-{ss['graded'] - ss['correct']} "
            f"({ss['accuracy']}%, avg +{ss['avg_pts_gained']} pts)")
