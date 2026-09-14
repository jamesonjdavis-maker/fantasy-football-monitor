# Fantasy Football Monitor

Daily watchdog for your **ESPN** and **Sleeper** fantasy leagues. It pulls your
roster, bench, and free agents from both platforms, saves a JSON snapshot,
diffs it against yesterday to catch *real* changes, flags start/sit and waiver
opportunities, and pings your phone via **ntfy** (or Discord) — but only when
something's actually worth your attention.

## What it does

- **Pulls** your roster, bench, and top free agents from ESPN (`espn-api`) and
  Sleeper (public API).
- **Snapshots** to `data/snapshot-YYYY-MM-DD.json` every run and **diffs**
  against the previous day to detect:
  - injury-status changes (e.g. `QUESTIONABLE → OUT`, extra-loud for starters)
  - roster adds/drops (waivers, trades)
  - a starter now on **bye** this week
  - free agents newly **trending** on the waiver wire
- **Analyzes** the current lineup for:
  - **bench players projected to outscore a starter** (start/sit nudges)
  - **ranked waiver targets** — a value score per available free agent blending
    projection, position scarcity (your weak spots), Monte Carlo ceiling, and
    recent hot form
- **Alerts** via a phone push (ntfy) or Discord embed — and stays silent on
  quiet days.
- **Runs on a schedule** via GitHub Actions, committing each snapshot back to
  the repo so day-over-day diffing works with zero external storage.
- **Keeps every credential in GitHub Secrets** — nothing sensitive is in code.

## Project layout

```
fantasy-football-monitor/
├── ffmonitor/
│   ├── config.py            # loads settings from env / Secrets
│   ├── models.py            # normalized cross-platform player shape
│   ├── platforms/
│   │   ├── espn.py          # espn-api wrapper (weekly box scores)
│   │   └── sleeper.py       # Sleeper public API client
│   ├── snapshot.py          # assemble today's snapshot
│   ├── storage.py           # save / load / prune snapshots
│   ├── diff.py              # day-over-day change events
│   ├── analyze.py           # bench>starter, weak-position pickups
│   ├── alerts.py            # ntfy / Discord formatting + send
│   └── main.py              # entry point: snapshot → diff → analyze → alert
├── .github/workflows/monitor.yml
├── data/                    # snapshots live here (committed by CI)
├── requirements.txt
└── .env.example
```

## Setup

### 1. Get your league identifiers

**Sleeper** (no login needed — the API is public):
- **League ID**: open your league on sleeper.app; it's in the URL
  `.../leagues/{LEAGUE_ID}/...`.
- **You**: set `SLEEPER_USERNAME` to your Sleeper handle (easiest), or
  `SLEEPER_USER_ID`. You can look up your ID at
  `https://api.sleeper.app/v1/user/YOUR_USERNAME` → `user_id`.

**ESPN**:
- **League ID**: from the URL `.../leagues/{LEAGUE_ID}`.
- **Team ID**: view your team; the URL includes `teamId=N`. (Optional — if you
  skip it the tool uses the first team, which is only right if you're team 1.)
- **Private leagues only** — two cookies from a logged-in `espn.com` session:
  1. In Chrome, sign in to ESPN, open **DevTools → Application → Cookies →
     `https://www.espn.com`**.
  2. Copy the values of **`espn_s2`** (long string) and **`SWID`** (looks like
     `{XXXXXXXX-....}`, braces included).
  - These are personal session cookies. Treat them like a password and put them
    in Secrets, never in code. They expire periodically — if ESPN stops working,
    refresh them.

> Tell me your league IDs and whether each is public or private and I'll tell
> you exactly which secrets you need to set.

### 2. Set up alerts (ntfy — free phone push)

1. Install the **ntfy** app (iOS/Android) or use [ntfy.sh](https://ntfy.sh) in a
   browser.
2. Pick an **unguessable topic name** — e.g. `jameson-ff-a7x9k2`. There are no
   accounts in ntfy; the topic name *is* the whole secret, and anyone who knows
   it can read your alerts, so don't use something obvious like `fantasy`.
3. In the app, **Subscribe** to that exact topic.
4. Use the same name as the `NTFY_TOPIC` secret.

Prefer Discord instead (or as well)? Create a webhook via **Server Settings →
Integrations → Webhooks → New Webhook → Copy Webhook URL** and set
`DISCORD_WEBHOOK_URL`. Set either or both; the tool sends to whatever's
configured.

### 3. Try it locally (optional but recommended)

```bash
cd fantasy-football-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
ALWAYS_NOTIFY=1 python -m ffmonitor.main   # forces a test alert
```

`ALWAYS_NOTIFY=1` makes it alert even when nothing's flagged, so you can confirm
your ntfy/Discord setup works. Drop it for normal runs. With no channel set, it
prints what it *would* have sent to the terminal.

### 4. Put it on GitHub with Actions

1. Create a GitHub repo and push this project.
2. Add your secrets: **Repo → Settings → Secrets and variables → Actions → New
   repository secret**. Set the ones you use:
   - `SLEEPER_LEAGUE_ID`, `SLEEPER_USERNAME` (or `SLEEPER_USER_ID`)
   - `ESPN_LEAGUES` = `Label:leagueId:teamId, …` (one line, all your ESPN
     leagues) — or a single `ESPN_LEAGUE_ID` + `ESPN_TEAM_ID`
   - `ESPN_S2`, `ESPN_SWID` (private leagues; **one pair covers all your
     leagues** — cookies are account-level, not per-league)
   - `NTFY_TOPIC` (and/or `DISCORD_WEBHOOK_URL`)
3. The workflow runs daily (13:00 UTC ≈ 9am ET, plus a Sunday-morning check).
   Trigger it by hand any time from **Actions → Fantasy Football Monitor → Run
   workflow** — tick *always_notify* there to test the webhook end-to-end.

The workflow has `contents: write` permission so it can commit each day's
snapshot back to `data/`. That committed history is what the next day's run
diffs against — no database or cloud storage needed.

## Tuning

Thresholds live in `ffmonitor/config.py` (`Thresholds`):

| Setting | Meaning | Default |
|---|---|---|
| `bench_over_starter_margin` | pts a bench player must beat a starter by | `2.0` |
| `weak_starter_proj` | starter projection below this = weak slot | `8.0` |
| `hot_add_min_count` | Sleeper 24h trending-add count to flag a FA | `3000` |
| `espn_hot_owned_pct` | ESPN ownership % to flag a FA | `40.0` |

Alert noise is controlled by the minimum severity in `main.run()` (`"low"` by
default). Raise it to `"medium"` to only hear about the bigger stuff.

## Machine learning (optional) — `ffmonitor/ml/`

A self-contained module for a **rising-usage / breakout signal**, built on
historical NFL game logs (`nfl_data_py`) — a *separate* data source from the
daily snapshots. The core monitor runs fine without any of this.

The idea: opportunity (targets + carries) moves *before* fantasy points do, so a
player whose share of their team's opportunity jumps above its recent baseline
is a buy-low candidate — often a week before platform projections react.

**Try the baseline signal:**

```bash
pip install -r requirements-ml.txt
python -m ffmonitor.ml.baseline               # top risers, latest week
python -m ffmonitor.ml.baseline --flagged-only --top 30
python -m ffmonitor.ml.baseline --seasons 2024 2025 --positions RB WR TE
```

It prints each player's current opportunity share, their trailing-3-week
baseline, the jump (Δ), and a 0–1 score. `★` marks players the rule flags
(Δ ≥ 8 pts of share, off a ≥10% share). This is deliberately a **rules-based
baseline first** — the number your future classifier has to beat.

- `ml/data.py` — pulls weekly stats and builds per-player-week usage features.
  The trailing average is **leakage-safe**: it uses only prior weeks, and never
  bleeds across seasons.
- `ml/baseline.py` — the rule, the 0–1 score, and `normalize_name()` (so
  nfl_data_py names line up with your ESPN/Sleeper roster — handles Jr./accents/
  initials).
- `ml/history.py` — pulls many **completed** seasons (default 8, set
  `HISTORY_SEASONS`) and derives a per-position **"notably rising" threshold**:
  how far a player's recent 3-week PPG must exceed their own season average to
  count, taken as the 75th percentile of historical rises.
- `ml/signal.py` — safe bridge; returns `None`/`{}` if the data pull or deps are
  unavailable, so the core run never breaks.

**The "heating up" signal (wired into alerts, `ENABLE_ML=1`):** history and
this-season deliberately share one unit — **fantasy points**:

- **History (nfl_data_py, completed seasons)** sets the bar per position.
- **This season (ESPN, live)** supplies each player's recent weekly points and
  season average — a *different, working* source, so the nflverse
  current-season gap doesn't block it.

`analyze.py` then flags **bench players and free agents whose recent form beats
their season average by more than the historical bar**, tagged
`🔥 Heating up` in your alert. Needs a few weeks of the current season played
before it fires (a player needs a recent stretch to outrun their average).
Turn it off by unsetting `ENABLE_ML`; the monitor runs fine without it.

**Monte Carlo floor/ceiling (`ml/montecarlo.py`, `ENABLE_ML=1`):** turns each
projection into a range instead of one number.

- From history, it learns each position's *performance multiplier* distribution
  (weekly points ÷ season average), **tiered by scoring level** so low-projected
  players are correctly boomier than studs.
- For a player projected `mu`, it simulates 5,000 outcomes = `mu ×` sampled
  multipliers and reads off **floor (P10) / median (P50) / ceiling (P90)** and a
  `safe floor` / `balanced` / `boom/bust` label. These attach to every roster
  player (and land in the snapshot JSON).
- Surfaced in alerts two ways: start/sit suggestions append both players'
  ranges, and a `🎲 Floor/ceiling` flag calls out a bench **upside dart** (higher
  ceiling despite an equal/lower median) or **safer floor** option.

- **Next step (not built yet):** a trained classifier (`train.py`/`predict.py`)
  can replace the percentile rules — but only once it beats these baselines.

## Notes & limits

- **Sleeper projections** come from an unofficial endpoint
  (`api.sleeper.com/projections/...`). If it's unavailable, projection-based
  flags are simply skipped that run — the tool degrades gracefully.
- **ESPN byes** are inferred from a starter projecting 0 points while otherwise
  active, since `espn-api` doesn't expose a bye field directly.
- The Sleeper player index (~5MB) is cached in `data/sleeper_players.json` for a
  day (gitignored) to avoid re-downloading each run.
- Exit code is `0` on normal runs (including "nothing to report") so a quiet day
  doesn't show as a failed Action; it's non-zero only if every league errors.
