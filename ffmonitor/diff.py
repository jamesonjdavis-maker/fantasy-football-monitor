"""Diff today's snapshot against the previous one to surface real changes.

Only *changes* produce events here — steady state is silent. This is what keeps
the alert from firing every day with the same roster.
"""

from __future__ import annotations

from typing import Any

# Severity ordering used for sorting and alert thresholds.
SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}

# Injury statuses that materially hurt a lineup.
_BAD_STATUSES = {"OUT", "IR", "DOUBTFUL", "SUSPENDED", "PUP"}


def _event(platform: str, kind: str, severity: str, message: str, **extra) -> dict:
    return {
        "platform": platform,
        "kind": kind,
        "severity": severity,
        "message": message,
        **extra,
    }


def _index_by_id(players: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in players or []}


def _diff_platform(name: str, prev: dict, curr: dict) -> list[dict]:
    events: list[dict] = []
    if not prev or not curr or curr.get("error") or prev.get("error"):
        return events

    prev_roster = _index_by_id(prev.get("roster", []))
    curr_roster = _index_by_id(curr.get("roster", []))

    # --- Injury status changes on rostered players ---
    for pid, cur in curr_roster.items():
        old = prev_roster.get(pid)
        if not old:
            continue
        if cur["injury_status"] != old["injury_status"]:
            got_worse = cur["injury_status"] in _BAD_STATUSES
            is_starter = cur["slot"] == "starter"
            severity = "high" if (got_worse and is_starter) else "medium"
            events.append(
                _event(
                    name,
                    "injury_change",
                    severity,
                    f"{cur['name']} ({cur['position']}) status "
                    f"{old['injury_status']} → {cur['injury_status']}"
                    + (" — STARTER" if is_starter else ""),
                    player=cur,
                )
            )

    # --- Roster adds / drops (waivers, trades) ---
    for pid, cur in curr_roster.items():
        if pid not in prev_roster:
            events.append(
                _event(
                    name,
                    "roster_add",
                    "medium",
                    f"Added {cur['name']} ({cur['position']}, {cur['pro_team']})",
                    player=cur,
                )
            )
    for pid, old in prev_roster.items():
        if pid not in curr_roster:
            events.append(
                _event(
                    name,
                    "roster_drop",
                    "info",
                    f"Dropped {old['name']} ({old['position']})",
                    player=old,
                )
            )

    # --- New byes hitting a starter this week ---
    for pid, cur in curr_roster.items():
        old = prev_roster.get(pid)
        if cur.get("on_bye") and cur["slot"] == "starter" and not (
            old and old.get("on_bye")
        ):
            events.append(
                _event(
                    name,
                    "bye_week",
                    "medium",
                    f"{cur['name']} ({cur['position']}) is on BYE this week "
                    f"and still in your starting lineup",
                    player=cur,
                )
            )

    # --- Newly trending free agents (didn't appear yesterday) ---
    prev_fa = _index_by_id(prev.get("free_agents", []))
    for pid, cur in _index_by_id(curr.get("free_agents", [])).items():
        if pid not in prev_fa:
            count = cur.get("percent_owned")
            detail = f" ({int(count):,} adds/24h)" if count else ""
            events.append(
                _event(
                    name,
                    "trending_fa",
                    "low",
                    f"Trending add available: {cur['name']} "
                    f"({cur['position']}, {cur['pro_team']}){detail}",
                    player=cur,
                )
            )

    return events


def diff_snapshots(prev: dict | None, curr: dict) -> list[dict]:
    """Return all change events across platforms, most severe first."""
    events: list[dict] = []
    if not prev:
        return events  # first run: nothing to diff against

    prev_platforms = prev.get("platforms", {})
    for name, cur_platform in curr.get("platforms", {}).items():
        events.extend(
            _diff_platform(name, prev_platforms.get(name, {}), cur_platform)
        )

    events.sort(key=lambda e: SEVERITY_ORDER.get(e["severity"], 0), reverse=True)
    return events
