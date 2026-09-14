"""Alerting: sends a notification only when there's something worth flagging.

Two backends, chosen by which secrets are set (you can enable both):
  * ntfy      — free phone push (set NTFY_TOPIC); default channel
  * Discord   — rich embed webhook (set DISCORD_WEBHOOK_URL)

If neither is configured, the alert is printed to stdout so the tool still
works locally without any push target.
"""

from __future__ import annotations

from typing import Any

import requests

_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🔵", "info": "⚪"}
_MAX_LINES_PER_GROUP = 12

# --- Discord specifics ---
_COLORS = {"high": 0xE03131, "medium": 0xF08C00, "low": 0x1971C2, "info": 0x2F9E44}
_MAX_FIELD_LEN = 1024

# --- ntfy specifics: severity -> priority (5=urgent … 1=min) ---
_NTFY_PRIORITY = {"high": "5", "medium": "4", "low": "3", "info": "3"}

_KIND_LABELS = {
    "injury_change": "🩹 Injury updates",
    "roster_add": "➕ Roster moves",
    "roster_drop": "➕ Roster moves",
    "bye_week": "🛑 Bye-week starters",
    "trending_fa": "📈 Trending free agents",
    "bench_over_starter": "🔁 Start/sit suggestions",
    "weak_position_fa": "🎯 Weak-position pickups",
    "ml_breakout_fa": "📈 Breakout watch (usage trending up)",
    "ml_breakout_bench": "📈 Breakout watch (usage trending up)",
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _sev_rank(sev: str) -> int:
    from .diff import SEVERITY_ORDER

    return SEVERITY_ORDER.get(sev, 0)


def _top_severity(items: list[dict]) -> str:
    top = "info"
    for it in items:
        if _sev_rank(it["severity"]) > _sev_rank(top):
            top = it["severity"]
    return top


def _grouped(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in items:
        out.setdefault(_KIND_LABELS.get(it.get("kind"), "ℹ️ Other"), []).append(it)
    return out


def _source_label(key: str | None) -> str:
    """Friendly league label from a source key: 'espn:BBL' -> 'BBL'."""
    if not key:
        return ""
    return key.split(":", 1)[1] if ":" in key else key.capitalize()


def _is_multi_source(items: list[dict]) -> bool:
    """True when the alert spans more than one league/platform, so each line
    should be tagged with which league it came from."""
    return len({it.get("platform") for it in items}) > 1


def _team_by_source(snapshot: dict) -> dict[str, str]:
    """Map source key -> your team name, so alert tags can show the team."""
    out: dict[str, str] = {}
    for key, snap in snapshot.get("platforms", {}).items():
        if isinstance(snap, dict) and snap.get("team_name"):
            out[key] = snap["team_name"]
    return out


def _tag(item: dict, multi: bool, teams: dict[str, str]) -> str:
    """Per-line prefix like '[BBL · Jamo] '. Empty for single-source alerts."""
    if not multi:
        return ""
    key = item.get("platform")
    label = _source_label(key)
    team = teams.get(key)
    return f"[{label} · {team}] " if team else f"[{label}] "


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
        label = snap.get("label") or _source_label(name)
        if snap.get("error"):
            parts.append(f"{label}: ⚠️ error")
        elif snap.get("enabled"):
            parts.append(f"{label}: {snap.get('team_name', 'team')}")
    return " • ".join(parts) or "Fantasy Football Monitor"


def _league_summary_lines(snapshot: dict) -> list[str]:
    """One line per league confirming it was checked — used for the 'all quiet'
    digest so you know every league was looked at even when there's no news."""
    lines: list[str] = []
    for key, snap in snapshot.get("platforms", {}).items():
        if not isinstance(snap, dict) or not snap.get("enabled"):
            continue
        name = snap.get("league_name") or snap.get("label") or _source_label(key)
        if snap.get("error"):
            lines.append(f"  ⚠️ {name}: error")
        else:
            team = snap.get("team_name", "your team")
            lines.append(f"  ✅ {name} — {team}")
    return lines


# --------------------------------------------------------------------------- #
# ntfy backend
# --------------------------------------------------------------------------- #
def build_ntfy(snapshot: dict, items: list[dict]) -> tuple[str, str, str]:
    """Return (title, body, priority) for an ntfy push. Body is UTF-8 plain
    text; the title is kept ASCII since ntfy headers dislike non-ASCII."""
    week = _first_week(snapshot)

    # Quiet digest: nothing flagged, but confirm every league was checked.
    if not items:
        summary = _league_summary_lines(snapshot)
        title = f"Daily digest - Week {week} - all quiet"
        body = "No roster changes or suggestions today.\n\nChecked:\n" + (
            "\n".join(summary) or "  (no leagues configured)"
        )
        return title, body, "2"  # low priority — it's a quiet heads-up

    title = f"Fantasy update - Week {week} ({len(items)} item{'s' if len(items) != 1 else ''})"

    multi = _is_multi_source(items)
    teams = _team_by_source(snapshot)
    lines: list[str] = []
    for label, group in _grouped(items).items():
        lines.append(label)
        for it in group[:_MAX_LINES_PER_GROUP]:
            emoji = _SEVERITY_EMOJI.get(it.get("severity", "info"), "•")
            lines.append(f"  {emoji} {_tag(it, multi, teams)}{it['message']}")
        if len(group) > _MAX_LINES_PER_GROUP:
            lines.append(f"  …and {len(group) - _MAX_LINES_PER_GROUP} more")
        lines.append("")
    body = "\n".join(lines).strip() or "Nothing to report."
    return title, body, _NTFY_PRIORITY.get(_top_severity(items), "3")


def send_ntfy(server: str, topic: str, title: str, body: str, priority: str) -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": "football",
        "Markdown": "no",
    }
    resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=30)
    resp.raise_for_status()
    return True


# --------------------------------------------------------------------------- #
# Discord backend
# --------------------------------------------------------------------------- #
def _discord_lines(items: list[dict], multi: bool, teams: dict[str, str]) -> str:
    lines = []
    for it in items[:_MAX_LINES_PER_GROUP]:
        emoji = _SEVERITY_EMOJI.get(it.get("severity", "info"), "•")
        lines.append(f"{emoji} {_tag(it, multi, teams)}{it['message']}")
    if len(items) > _MAX_LINES_PER_GROUP:
        lines.append(f"…and {len(items) - _MAX_LINES_PER_GROUP} more")
    return ("\n".join(lines) or "—")[:_MAX_FIELD_LEN]


def build_discord_embed(snapshot: dict, items: list[dict]) -> dict:
    week = _first_week(snapshot)
    if not items:
        summary = _league_summary_lines(snapshot)
        embed = {
            "title": f"🏈 Daily digest — Week {week} · all quiet",
            "description": "No roster changes or suggestions today.",
            "color": 0x2F9E44,
            "fields": [
                {
                    "name": "Checked",
                    "value": ("\n".join(summary) or "—")[:_MAX_FIELD_LEN],
                    "inline": False,
                }
            ],
            "footer": {"text": _footer(snapshot)},
            "timestamp": snapshot.get("generated_at"),
        }
        return {"username": "Fantasy Football Monitor", "embeds": [embed]}

    multi = _is_multi_source(items)
    teams = _team_by_source(snapshot)
    fields = [
        {"name": label, "value": _discord_lines(group, multi, teams), "inline": False}
        for label, group in _grouped(items).items()
    ]
    return {
        "username": "Fantasy Football Monitor",
        "embeds": [
            {
                "title": f"🏈 Fantasy update — Week {week}",
                "description": f"{len(items)} item(s) worth a look.",
                "color": _COLORS.get(_top_severity(items), 0x2F9E44),
                "fields": fields[:25],
                "footer": {"text": _footer(snapshot)},
                "timestamp": snapshot.get("generated_at"),
            }
        ],
    }


def send_discord(webhook_url: str, embed: dict) -> bool:
    resp = requests.post(webhook_url, json=embed, timeout=30)
    resp.raise_for_status()
    return True


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def notify(config, snapshot: dict, events: list[dict], flags: list[dict]) -> None:
    """Send to every configured channel; fall back to stdout if none set."""
    items = events + flags
    sent_any = False

    if getattr(config, "ntfy_topic", None):
        title, body, priority = build_ntfy(snapshot, items)
        try:
            send_ntfy(config.ntfy_server, config.ntfy_topic, title, body, priority)
            print(f"[alerts] ntfy push sent to topic '{config.ntfy_topic}'.")
            sent_any = True
        except requests.RequestException as exc:
            print(f"[alerts] ntfy send failed: {exc}")

    if getattr(config, "discord_webhook_url", None):
        embed = build_discord_embed(snapshot, items)
        try:
            send_discord(config.discord_webhook_url, embed)
            print("[alerts] Discord alert sent.")
            sent_any = True
        except requests.RequestException as exc:
            print(f"[alerts] Discord send failed: {exc}")

    if not sent_any:
        title, body, _ = build_ntfy(snapshot, items)
        print(f"[alerts] No push channel configured. Would have sent:\n{title}\n{body}")
