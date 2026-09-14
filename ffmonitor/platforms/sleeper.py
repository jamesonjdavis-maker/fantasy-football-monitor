"""Sleeper platform client.

Sleeper's read API is public and unauthenticated, so no cookies or tokens are
needed — only the league ID and a way to identify which roster is yours
(user_id or username).

Endpoints used (all GET, no auth):
  https://api.sleeper.app/v1/state/nfl                     -> current week/season
  https://api.sleeper.app/v1/league/{league_id}            -> scoring settings
  https://api.sleeper.app/v1/league/{league_id}/rosters    -> every roster
  https://api.sleeper.app/v1/league/{league_id}/users      -> owner display names
  https://api.sleeper.app/v1/user/{username}               -> user_id lookup
  https://api.sleeper.app/v1/players/nfl                    -> player metadata (~5MB)
  https://api.sleeper.app/v1/players/nfl/trending/add       -> trending waiver adds

Weekly projections come from the newer api.sleeper.com host, which is
community-known but unofficial; failures there degrade gracefully (projections
just become None and projection-based flags are skipped).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from ..config import SleeperConfig
from ..models import BENCH, IR, STARTER, normalize_player

_V1 = "https://api.sleeper.app/v1"
_V2 = "https://api.sleeper.com"  # projections/stats live here
_TIMEOUT = 30


def _get(url: str, *, params: dict | None = None) -> Any:
    resp = requests.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_nfl_state() -> dict[str, Any]:
    """Current week / season / season_type."""
    return _get(f"{_V1}/state/nfl")


def _load_player_index(cache_dir: Path) -> dict[str, Any]:
    """The players/nfl payload is large (~5MB) and changes slowly, so cache it
    to disk for a day and reuse it across runs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "sleeper_players.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86_400:
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    data = _get(f"{_V1}/players/nfl")
    try:
        cache.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def _resolve_user_id(cfg: SleeperConfig) -> str | None:
    if cfg.user_id:
        return cfg.user_id
    if cfg.username:
        try:
            user = _get(f"{_V1}/user/{cfg.username}")
            return user.get("user_id")
        except requests.RequestException:
            return None
    return None


def _scoring_key(league: dict[str, Any]) -> str:
    """Pick the projection field that matches the league's PPR setting."""
    rec = (league.get("scoring_settings") or {}).get("rec", 0)
    if rec >= 1.0:
        return "pts_ppr"
    if rec >= 0.5:
        return "pts_half_ppr"
    return "pts_std"


def _fetch_projections(season: str, week: int, scoring_key: str) -> dict[str, float]:
    """Best-effort weekly projections keyed by Sleeper player_id."""
    try:
        data = _get(
            f"{_V2}/projections/nfl/{season}/{week}",
            params={"season_type": "regular"},
        )
    except requests.RequestException:
        return {}
    out: dict[str, float] = {}
    for row in data or []:
        pid = str(row.get("player_id"))
        stats = row.get("stats") or {}
        val = stats.get(scoring_key)
        if val is not None:
            out[pid] = float(val)
    return out


def _player_from_meta(
    pid: str,
    meta: dict[str, Any],
    *,
    slot: str,
    proj: float | None,
) -> dict[str, Any]:
    full = meta.get("full_name") or " ".join(
        p for p in (meta.get("first_name"), meta.get("last_name")) if p
    )
    return normalize_player(
        player_id=pid,
        name=full or pid,
        position=meta.get("position"),
        pro_team=meta.get("team"),
        slot=slot,
        injury_status=meta.get("injury_status"),
        proj_points=proj,
        avg_points=None,
        percent_owned=None,
    )


def build_snapshot(cfg: SleeperConfig, cache_dir: Path) -> dict[str, Any]:
    """Return a normalized snapshot for the Sleeper league, or an error dict."""
    if not cfg.enabled:
        return {"enabled": False}

    try:
        state = get_nfl_state()
        season = str(state.get("season"))
        week = int(state.get("week") or 1)

        league = _get(f"{_V1}/league/{cfg.league_id}")
        rosters = _get(f"{_V1}/league/{cfg.league_id}/rosters")
        players_meta = _load_player_index(cache_dir)
    except requests.RequestException as exc:
        return {"enabled": True, "error": f"Sleeper API error: {exc}"}

    user_id = _resolve_user_id(cfg)
    if not user_id:
        return {
            "enabled": True,
            "error": "Could not resolve Sleeper user_id. Set SLEEPER_USER_ID "
            "or SLEEPER_USERNAME.",
        }

    my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)
    if my_roster is None:
        return {
            "enabled": True,
            "error": f"No roster in league {cfg.league_id} owned by user {user_id}.",
        }

    scoring_key = _scoring_key(league)
    projections = _fetch_projections(season, week, scoring_key)

    # Every player_id rostered anywhere in the league (to find true free agents).
    rostered: set[str] = set()
    for r in rosters:
        for pid in r.get("players") or []:
            rostered.add(str(pid))

    starters = {str(p) for p in (my_roster.get("starters") or []) if p and p != "0"}
    reserve = {str(p) for p in (my_roster.get("reserve") or [])}
    all_mine = [str(p) for p in (my_roster.get("players") or [])]

    roster: list[dict[str, Any]] = []
    for pid in all_mine:
        meta = players_meta.get(pid, {})
        if pid in starters:
            slot = STARTER
        elif pid in reserve:
            slot = IR
        else:
            slot = BENCH
        roster.append(
            _player_from_meta(pid, meta, slot=slot, proj=projections.get(pid))
        )

    # Free agents: trending adds that nobody in the league rosters.
    free_agents = _fetch_free_agents(rostered, players_meta, projections)

    return {
        "enabled": True,
        "league_id": str(cfg.league_id),
        "league_name": league.get("name"),
        "team_name": _team_name(rosters, my_roster, cfg, user_id),
        "week": week,
        "season": season,
        "roster": roster,
        "free_agents": free_agents,
    }


def _team_name(
    rosters: list[dict], my_roster: dict, cfg: SleeperConfig, user_id: str
) -> str:
    try:
        users = _get(f"{_V1}/league/{cfg.league_id}/users")
        me = next((u for u in users if u.get("user_id") == user_id), None)
        if me:
            meta = me.get("metadata") or {}
            return meta.get("team_name") or me.get("display_name") or "My Team"
    except requests.RequestException:
        pass
    return "My Team"


def _fetch_free_agents(
    rostered: set[str],
    players_meta: dict[str, Any],
    projections: dict[str, float],
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        trending = _get(
            f"{_V1}/players/nfl/trending/add",
            params={"lookback_hours": 24, "limit": limit},
        )
    except requests.RequestException:
        return []
    out: list[dict[str, Any]] = []
    for row in trending or []:
        pid = str(row.get("player_id"))
        if pid in rostered:
            continue  # already owned by someone in the league
        meta = players_meta.get(pid, {})
        player = _player_from_meta(
            pid, meta, slot="free_agent", proj=projections.get(pid)
        )
        # Reuse percent_owned to carry the trending add count across platforms.
        player["percent_owned"] = float(row.get("count") or 0)
        out.append(player)
    return out
