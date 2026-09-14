"""Persist daily snapshots and load the previous one for diffing.

Snapshots are written as `data/snapshot-YYYY-MM-DD.json` plus a `latest.json`
pointer. The "previous" snapshot for diffing is the most recent dated file that
is *not* today's — so re-running on the same day compares against yesterday, not
against the run from ten minutes ago.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

_SNAPSHOT_RE = re.compile(r"snapshot-(\d{4}-\d{2}-\d{2})\.json$")


def _snapshot_path(data_dir: Path, day: date) -> Path:
    return data_dir / f"snapshot-{day.isoformat()}.json"


def save(data_dir: Path, snapshot: dict[str, Any], day: date | None = None) -> Path:
    day = day or date.today()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(data_dir, day)
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    path.write_text(payload)
    (data_dir / "latest.json").write_text(payload)
    return path


def load_previous(data_dir: Path, before: date | None = None) -> dict[str, Any] | None:
    """Most recent dated snapshot strictly before `before` (default: today)."""
    before = before or date.today()
    if not data_dir.exists():
        return None

    dated: list[tuple[date, Path]] = []
    for path in data_dir.glob("snapshot-*.json"):
        m = _SNAPSHOT_RE.search(path.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < before:
            dated.append((d, path))

    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    _, latest = dated[-1]
    try:
        return json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def prune(data_dir: Path, keep: int = 30) -> None:
    """Keep only the most recent `keep` dated snapshots."""
    paths = sorted(data_dir.glob("snapshot-*.json"))
    for path in paths[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass
