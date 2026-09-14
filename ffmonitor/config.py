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
class ESPNConfig:
    league_id: int | None
    team_id: int | None
    espn_s2: str | None
    swid: str | None
    year: int

    @property
    def enabled(self) -> bool:
        return self.league_id is not None

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


@dataclass
class Config:
    espn: ESPNConfig
    sleeper: SleeperConfig
    discord_webhook_url: str | None
    ntfy_topic: str | None
    ntfy_server: str
    data_dir: Path
    thresholds: Thresholds = field(default_factory=Thresholds)
    # If True, send the Discord alert even when nothing is flagged (useful for
    # a first run / testing the webhook).
    always_notify: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        return cls(
            espn=ESPNConfig(
                league_id=_int_or_none(os.getenv("ESPN_LEAGUE_ID")),
                team_id=_int_or_none(os.getenv("ESPN_TEAM_ID")),
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
        )
