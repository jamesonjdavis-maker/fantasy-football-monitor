"""NFL point spreads from ESPN's public scoreboard API (no key required).

Used for game-script signals: a heavy favorite tends to run more (RB-friendly),
a heavy underdog throws to catch up (RBs fade, pass-catchers get garbage-time
volume). Spreads are returned per team abbreviation, negative = favored.
"""

from __future__ import annotations

import re

import requests

_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_TIMEOUT = 30

# Normalize the few abbreviations that differ between ESPN's feeds.
_ALIASES = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR"}

_DETAILS_RE = re.compile(r"^([A-Z]{2,4})\s+(-?\d+(?:\.\d+)?)$")


def _norm(abbr: str | None) -> str | None:
    if not abbr:
        return None
    a = abbr.upper()
    return _ALIASES.get(a, a)


def fetch_odds(week: int | None = None, season: int | None = None) -> dict[str, dict]:
    """Return {team_abbr: {"spread", "total", "implied_total"}} for the slate.

    spread is negative when favored; total is the game's over/under; implied_total
    is how many points Vegas expects that team to score, derived as
    (total - spread) / 2. Empty on any failure so the monitor is never blocked.
    """
    params: dict[str, str] = {}
    if week:
        params["week"] = str(week)
    if season:
        params["dates"] = str(season)
    try:
        data = requests.get(_SCOREBOARD, params=params, timeout=_TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return {}

    out: dict[str, dict] = {}
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            competitors = comp.get("competitors", [])
            odds = comp.get("odds") or []
            if len(competitors) < 2 or not odds:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            home_abbr = _norm((home.get("team") or {}).get("abbreviation"))
            away_abbr = _norm((away.get("team") or {}).get("abbreviation"))

            total = odds[0].get("overUnder")
            try:
                total = float(total) if total is not None else None
            except (TypeError, ValueError):
                total = None

            details = (odds[0].get("details") or "").strip()
            if details.upper() in ("EVEN", "PK", "PICK"):
                home_spread = away_spread = 0.0
            else:
                m = _DETAILS_RE.match(details)
                if not m:
                    continue
                fav = _norm(m.group(1))
                line = float(m.group(2))  # negative, e.g. -7.5
                home_spread = line if fav == home_abbr else -line
                away_spread = -home_spread

            for abbr, spread in ((home_abbr, home_spread), (away_abbr, away_spread)):
                implied = round((total - spread) / 2, 1) if total is not None else None
                out[abbr] = {"spread": spread, "total": total, "implied_total": implied}
    return out


def fetch_spreads(week: int | None = None, season: int | None = None) -> dict[str, float]:
    """Return {team_abbr: spread}, negative = favored. Thin wrapper over
    fetch_odds, kept for the game-script flags."""
    return {t: v["spread"] for t, v in fetch_odds(week, season).items()}


def fetch_implied_totals(week: int | None = None, season: int | None = None) -> dict[str, float]:
    """Return {team_abbr: implied point total} — how much Vegas expects the team
    to score this week."""
    return {t: v["implied_total"] for t, v in fetch_odds(week, season).items()
            if v.get("implied_total") is not None}
