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


def fetch_spreads(week: int | None = None, season: int | None = None) -> dict[str, float]:
    """Return {team_abbr: spread}, negative = favored. Empty on any failure so
    the monitor is never blocked by the odds feed."""
    params: dict[str, str] = {}
    if week:
        params["week"] = str(week)
    if season:
        params["dates"] = str(season)
    try:
        data = requests.get(_SCOREBOARD, params=params, timeout=_TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return {}

    spreads: dict[str, float] = {}
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

            details = (odds[0].get("details") or "").strip()
            if details.upper() in ("EVEN", "PK", "PICK"):
                spreads[home_abbr] = 0.0
                spreads[away_abbr] = 0.0
                continue
            m = _DETAILS_RE.match(details)
            if not m:
                continue
            fav = _norm(m.group(1))
            line = float(m.group(2))  # negative, e.g. -7.5
            other = away_abbr if fav == home_abbr else home_abbr
            spreads[fav] = line
            spreads[other] = -line
    return spreads
