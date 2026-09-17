"""Configuration loaded from environment variables (GitHub Secrets in CI).

Nothing sensitive is hardcoded. Locally, values are read from a `.env` file
via python-dotenv; in GitHub Actions they come from the workflow `env:` block
which is populated from repository Secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime (only needed locally)
    pass


def _default_season() -> int:
    """NFL season year. The season spans Sep–Feb, so before August we're
    still in the prior season's playoffs."""
    today = date.today()
    return today.year if today.month >= 8 else today.year - 1


def _clean_swid(swid: str | None) -> str | None:
    """ESPN's SWID cookie is usually wrapped in braces. espn-api wants it
    with the braces, so we normalize to that form regardless of how it was
    pasted into the secret."""
    if not swid:
        return None
    swid = swid.strip()
    if not swid.startswith("{"):
        swid = "{" + swid
    if not swid.endswith("}"):
        swid = swid + "}"
    return swid


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


@dataclass
class ESPNLeague:
    """One ESPN league to monitor. The cookies are account-level (shared across
    all leagues), so only the league_id, team_id, and a friendly label vary."""
    label: str
    league_id: int
    team_id: int | None


def _parse_espn_leagues(
    raw: str | None, fallback_id: int | None, fallback_team: int | None
) -> list[ESPNLeague]:
    """Parse ESPN_LEAGUES ("Label:leagueId:teamId, Label2:leagueId2:teamId2").

    teamId is optional per entry. Falls back to the single ESPN_LEAGUE_ID /
    ESPN_TEAM_ID pair (labeled "ESPN") when ESPN_LEAGUES isn't set, so older
    single-league setups keep working unchanged.
    """
    leagues: list[ESPNLeague] = []
    if raw:
        for chunk in raw.split(","):
            parts = [p.strip() for p in chunk.split(":")]
            if len(parts) < 2 or not parts[0]:
                continue
            lid = _int_or_none(parts[1])
            if lid is None:
                continue
            tid = _int_or_none(parts[2]) if len(parts) >= 3 else None
            leagues.append(ESPNLeague(label=parts[0], league_id=lid, team_id=tid))
    if not leagues and fallback_id is not None:
        leagues.append(
            ESPNLeague(label="ESPN", league_id=fallback_id, team_id=fallback_team)
        )
    return leagues


@dataclass
class ESPNConfig:
    leagues: list[ESPNLeague]
    espn_s2: str | None
    swid: str | None
    year: int

    @property
    def enabled(self) -> bool:
        return len(self.leagues) > 0

    @property
    def is_private(self) -> bool:
        return bool(self.espn_s2 and self.swid)


@dataclass
class SleeperConfig:
    league_id: str | None
    user_id: str | None
    username: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.league_id)


@dataclass
class Thresholds:
    # A bench player is flagged over a starter only if it beats the starter's
    # weekly projection by at least this many points (avoids noise from ties).
    bench_over_starter_margin: float = 2.0
    # A starting slot is "weak" if the starter is projected below this.
    weak_starter_proj: float = 8.0
    # Minimum trending-add count (Sleeper) for a free agent to be "hot".
    hot_add_min_count: int = 3000
    # Minimum ESPN percent_owned change to flag a free agent as rising.
    espn_hot_owned_pct: float = 40.0
    # Point-spread size (favored or underdog) that triggers a game-script flag.
    game_script_spread: float = 7.0


@dataclass
class Config:
    espn: ESPNConfig
    sleeper: SleeperConfig
    discord_webhook_url: str | None
    ntfy_topic: str | None
    ntfy_server: str
    data_dir: Path
    thresholds: Thresholds = field(default_factory=Thresholds)
    # If True, send the alert even when nothing is flagged (daily digest /
    # first-run webhook test).
    always_notify: bool = False
    # If True, load the ML rising-usage signal (needs requirements-ml.txt). Off
    # by default so the core monitor stays lightweight and dependency-free.
    enable_ml: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        return cls(
            espn=ESPNConfig(
                leagues=_parse_espn_leagues(
                    os.getenv("ESPN_LEAGUES"),
                    _int_or_none(os.getenv("ESPN_LEAGUE_ID")),
                    _int_or_none(os.getenv("ESPN_TEAM_ID")),
                ),
                espn_s2=os.getenv("ESPN_S2") or None,
                swid=_clean_swid(os.getenv("ESPN_SWID")),
                year=_int_or_none(os.getenv("ESPN_YEAR")) or _default_season(),
            ),
            sleeper=SleeperConfig(
                league_id=os.getenv("SLEEPER_LEAGUE_ID") or None,
                user_id=os.getenv("SLEEPER_USER_ID") or None,
                username=os.getenv("SLEEPER_USERNAME") or None,
            ),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
            ntfy_topic=os.getenv("NTFY_TOPIC") or None,
            ntfy_server=os.getenv("NTFY_SERVER") or "https://ntfy.sh",
            data_dir=data_dir,
            always_notify=os.getenv("ALWAYS_NOTIFY", "").lower()
            in ("1", "true", "yes"),
            enable_ml=os.getenv("ENABLE_ML", "").lower() in ("1", "true", "yes"),
        )
