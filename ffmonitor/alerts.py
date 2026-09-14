"""Discord webhook alerting.

Sends a single rich embed only when there's something worth flagging. The
webhook URL comes from the DISCORD_WEBHOOK_URL secret; if it's missing we print
to stdout instead so the tool still works locally without a webhook.
"""

from __future__ import annotations

from typing import Any

import requests

# Discord embed colors (decimal) by top severity.
_COLORS = {"high": 0xE03131, "medium": 0xF08C00, "low": 0x1971C2, "info": 0x2F9E44}
_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🔵", "info": "⚪"}
_MAX_FIELD_LEN = 1024
_MAX_LINES_PER_GROUP = 12


def _group(items: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in items:
        out.setdefault(it.get(key, "?"), []).append(it)
    return out


def _lines(items: list[dict]) -> str:
    lines = []
    for it in items[:_MAX_LINES_PER_GROUP]:
        emoji = _SEVERITY_EMOJI.get(it.get("severity", "info"), "•")
        lines.append(f"{emoji} {it['message']}")
    if len(items) > _MAX_LINES_PER_GROUP:
        lines.append(f"…and {len(items) - _MAX_LINES_PER_GROUP} more")
    text = "\n".join(lines) or "—"
    return text[:_MAX_FIELD_LEN]


_KIND_LABELS = {
    "injury_change": "🩹 Injury updates",
    "roster_add": "➕ Roster moves",
    "roster_drop": "➕ Roster moves",
    "bye_week": "🛑 Bye-week starters",
    "trending_fa": "📈 Trending free agents",
    "bench_over_starter": "🔁 Start/sit suggestions",
    "weak_position_fa": "🎯 Weak-position pickups",
}


def build_embed(snapshot: dict, events: list[dict], flags: list[dict]) -> dict:
    items = events + flags
    top_severity = "info"
    for it in items:
        if _sev_rank(it["severity"]) > _sev_rank(top_severity):
            top_severity = it["severity"]

    # Group all items by their display label.
    grouped: dict[str, list[dict]] = {}
    for it in items:
        label = _KIND_LABELS.get(it.get("kind"), "ℹ️ Other")
        grouped.setdefault(label, []).append(it)

    fields = []
    for label, group in grouped.items():
        fields.append({"name": label, "value": _lines(group), "inline": False})

    week = _first_week(snapshot)
    return {
        "username": "Fantasy Football Monitor",
        "embeds": [
            {
                "title": f"🏈 Fantasy update — Week {week}",
                "description": f"{len(items)} item(s) worth a look.",
                "color": _COLORS.get(top_severity, 0x2F9E44),
                "fields": fields[:25],  # Discord caps embeds at 25 fields
                "footer": {"text": _footer(snapshot)},
                "timestamp": snapshot.get("generated_at"),
            }
        ],
    }


def _sev_rank(sev: str) -> int:
    from .diff import SEVERITY_ORDER

    return SEVERITY_ORDER.get(sev, 0)


def _first_week(snapshot: dict) -> Any:
    for snap in snapshot.get("platforms", {}).values():
        if isinstance(snap, dict) and snap.get("week"):
            return snap["week"]
    return "?"


def _footer(snapshot: dict) -> str:
    parts = []
    for name, snap in snapshot.get("platforms", {}).items():
        if not isinstance(snap, dict):
            continue
        if snap.get("error"):
            parts.append(f"{name}: ⚠️ error")
        elif snap.get("enabled"):
            parts.append(f"{name}: {snap.get('team_name', 'team')}")
    return " • ".join(parts) or "Fantasy Football Monitor"


def send_discord(webhook_url: str, embed: dict) -> bool:
    """POST the embed to Discord. Returns True on success."""
    resp = requests.post(webhook_url, json=embed, timeout=30)
    resp.raise_for_status()
    return True


def notify(
    webhook_url: str | None, snapshot: dict, events: list[dict], flags: list[dict]
) -> None:
    embed = build_embed(snapshot, events, flags)
    if not webhook_url:
        print("[alerts] No DISCORD_WEBHOOK_URL set; would have sent:")
        for e in embed["embeds"]:
            for f in e.get("fields", []):
                print(f"  {f['name']}\n    " + f["value"].replace("\n", "\n    "))
        return
    send_discord(webhook_url, embed)
    print("[alerts] Discord alert sent.")
