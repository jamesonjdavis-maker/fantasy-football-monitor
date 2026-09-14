"""Entry point: build snapshot → diff → analyze → alert if worthwhile.

Run with:  python -m ffmonitor.main
Exit code is always 0 on a normal run (including "nothing to report") so a
GitHub Actions schedule isn't marked failed on quiet days. Hard failures
(all platforms errored) exit non-zero.
"""

from __future__ import annotations

import sys

from . import analyze, diff, snapshot, storage
from .alerts import notify
from .config import Config
from .diff import SEVERITY_ORDER


def _worth_alerting(events: list[dict], flags: list[dict], min_severity: str) -> bool:
    floor = SEVERITY_ORDER[min_severity]
    return any(
        SEVERITY_ORDER.get(it["severity"], 0) >= floor for it in events + flags
    )


def run(config: Config, min_severity: str = "low") -> int:
    current = snapshot.build(config)

    # Detect a total failure (every enabled platform errored out).
    platforms = current.get("platforms", {})
    enabled = [p for p in platforms.values() if p.get("enabled")]
    if enabled and all(p.get("error") for p in enabled):
        for name, p in platforms.items():
            if p.get("error"):
                print(f"[error] {name}: {p['error']}", file=sys.stderr)
        return 1

    previous = storage.load_previous(config.data_dir)
    events = diff.diff_snapshots(previous, current)

    # ML rising-usage signal (optional; safe no-op if deps/data unavailable).
    rising_scores: dict = {}
    if config.enable_ml:
        from .ml import signal as ml_signal

        rising_scores = ml_signal.get_rising_scores()
    flags = analyze.analyze(current, config.thresholds, rising_scores=rising_scores)

    path = storage.save(config.data_dir, current)
    storage.prune(config.data_dir)
    print(f"[snapshot] saved {path} ({len(events)} events, {len(flags)} flags)")

    # Surface any per-platform errors without aborting the whole run.
    for name, p in platforms.items():
        if p.get("error"):
            print(f"[warn] {name}: {p['error']}", file=sys.stderr)

    if config.always_notify or _worth_alerting(events, flags, min_severity):
        notify(config, current, events, flags)
    else:
        print("[alerts] Nothing worth flagging today. Staying quiet.")

    return 0


def main() -> None:
    config = Config.from_env()
    if not (config.espn.enabled or config.sleeper.enabled):
        print(
            "[config] No leagues configured. Set ESPN_LEAGUE_ID and/or "
            "SLEEPER_LEAGUE_ID.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(run(config))


if __name__ == "__main__":
    main()
