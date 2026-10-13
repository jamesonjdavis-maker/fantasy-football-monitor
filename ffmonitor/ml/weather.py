"""Weather adjustment for weekly projections.

Bad weather mainly hurts the passing and kicking game: strong wind, heavy
precipitation, and extreme cold cut into pass-catchers' and QBs' output, while
running backs are far less affected. Domes (and fixed-roof stadiums) are immune.

Forecasts only exist a couple of weeks out, so this is a near-term layer: games
beyond the forecast horizon, in a dome, or with no data return a neutral 1.0.
Data is the free Open-Meteo forecast API (no key). Everything is best-effort —
any failure yields 1.0 so the projection is never blocked.
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 20
_HORIZON_DAYS = 14  # Open-Meteo forecasts ~16 days; past that we don't call.

# Home stadium per team: (lat, lon, dome?). Dome=True covers fixed and
# retractable roofs and fixed-roof open-air (SoFi) — effectively weather-immune.
_STADIUMS: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.5277, -112.2626, True),
    "ATL": (33.7554, -84.4009, True),
    "BAL": (39.2780, -76.6227, False),
    "BUF": (42.7738, -78.7870, False),
    "CAR": (35.2258, -80.8528, False),
    "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0954, -84.5160, False),
    "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True),
    "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True),
    "GB": (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True),
    "IND": (39.7601, -86.1639, True),
    "JAX": (30.3239, -81.6373, False),
    "KC": (39.0489, -94.4839, False),
    "LV": (36.0909, -115.1833, True),
    "LAC": (33.9535, -118.3392, True),
    "LAR": (33.9535, -118.3392, True),
    "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9736, -93.2575, True),
    "NE": (42.0909, -71.2643, False),
    "NO": (29.9511, -90.0812, True),
    "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False),
    "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False),
    "SEA": (47.5952, -122.3316, False),
    "SF": (37.4030, -121.9700, False),
    "TB": (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False),
    "WAS": (38.9076, -76.8645, False),
}


def _within_horizon(date_str: str) -> bool:
    try:
        d = _dt.date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return False
    delta = (d - _dt.date.today()).days
    return 0 <= delta <= _HORIZON_DAYS


@lru_cache(maxsize=256)
def _forecast(lat: float, lon: float, date_str: str) -> tuple[float | None, float | None, float | None]:
    """(max wind mph, precip inches, min temp F) for a venue/day, or Nones."""
    try:
        import requests
        resp = requests.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "wind_speed_10m_max,precipitation_sum,temperature_2m_min",
                "start_date": date_str,
                "end_date": date_str,
                "timezone": "auto",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "temperature_unit": "fahrenheit",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        daily = (resp.json() or {}).get("daily") or {}

        def _first(key):
            vals = daily.get(key) or []
            return float(vals[0]) if vals and vals[0] is not None else None

        return _first("wind_speed_10m_max"), _first("precipitation_sum"), _first("temperature_2m_min")
    except Exception:
        return None, None, None


def weather_factor(venue_team: str | None, date_str: str | None, pos: str) -> float:
    """Multiplier in [0.75, 1.0] for one game. 1.0 for domes, missing data, or
    games past the forecast horizon. Pass-catchers/QBs take the full hit; RBs a
    fraction of it."""
    if not venue_team or not date_str:
        return 1.0
    stadium = _STADIUMS.get(str(venue_team).upper())
    if not stadium:
        return 1.0
    lat, lon, dome = stadium
    if dome or not _within_horizon(date_str):
        return 1.0
    wind, precip, tmin = _forecast(lat, lon, date_str[:10])
    pen = 0.0
    if wind is not None and wind > 15:
        pen += min((wind - 15) * 0.012, 0.18)   # wind is the big one for passing
    if precip is not None and precip > 0.1:
        pen += min(precip * 0.05, 0.08)
    if tmin is not None and tmin < 20:
        pen += 0.04
    if pen <= 0:
        return 1.0
    # RBs are far less weather-sensitive than the passing game.
    pen = pen if pos in ("QB", "WR", "TE") else pen * 0.4
    return max(0.75, min(1.0, 1.0 - pen))
