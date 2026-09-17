# Fantasy Football Monitor: Detailed Build Report

This is the deep version. It walks through how every part of the project was
built, the reasoning behind each choice, and the problems solved along the way. It
is written to be understandable without a programming background, while still
being specific about what actually happens under the hood.

For the short overview, see [REPORT.md](REPORT.md). For setup instructions, see
[README.md](README.md).

---

## Table of contents

1. The big picture
2. How the pieces fit together
3. Collecting the data (snapshots)
4. Finding what changed (diffing)
5. The analysis: start/sit, waivers, game script
6. The machine learning, explained
7. Proving it works (backtesting and model quality)
8. Grading itself in real time (live outcomes)
9. Sending alerts
10. The AI assistant (Discord bot)
11. Trade evaluation
12. Engineering practices
13. Problems we hit and how we fixed them
14. Honest limitations
15. Glossary

---

## 1. The big picture

The tool answers a simple question every morning: "Is there anything in my five
fantasy leagues I should act on today?" To do that well, it has to know the
current state of each league, notice what changed, judge whether any change or
opportunity is worth my attention, and then tell me, without crying wolf on quiet
days.

The design principle throughout is **graceful degradation**: the core monitor is
simple and reliable, and every fancier feature is bolted on in a way that can fail
quietly without breaking the basics. If the machine-learning data is down, the
start/sit advice still works. If the odds feed is down, everything else still runs.

## 2. How the pieces fit together

The code is a Python package called `ffmonitor`, split into focused files:

- **config** loads settings and secrets from the environment.
- **models** defines one standard "player" shape so every league looks identical to
  the rest of the code.
- **platforms/espn** talks to ESPN and builds a snapshot of a league.
- **snapshot / storage** assemble and save the daily snapshot, and load yesterday's.
- **diff** compares two days to produce a list of real changes.
- **analyze** runs the point-in-time checks (start/sit, waivers, game script).
- **ml** holds the machine-learning models (Monte Carlo, value over replacement,
  the heating-up signal).
- **odds** pulls betting lines.
- **outcomes** logs the tool's recommendations and grades them later.
- **eval** is the offline backtesting and model-quality harness.
- **alerts** formats and sends the message.
- **main** is the conductor that runs the whole sequence.

A separate `discord-bot` folder holds the phone assistant, which is a small cloud
function written in JavaScript.

The daily run, in order: build today's snapshot, load yesterday's, diff them, fetch
odds, run the machine-learning add-ons, run the analysis, log and grade
recommendations, save today's snapshot, and send an alert if warranted.

## 3. Collecting the data (snapshots)

Every run, for each league, the tool logs into ESPN (using account cookies for
private leagues) and reads:

- My starting lineup and bench, with each player's weekly projection, position,
  injury status, and recent scores.
- This week's opponent.
- The league's roster rules (how many of each position start), read directly from
  my lineup so the tool adapts to each league automatically.
- The top available free agents, cross-checked against every team's roster so it
  never suggests a player who is actually owned.
- Every team's full roster (used later for trade evaluation).

All of this is normalized into a plain, standard format and saved as a dated file
like `snapshot-2026-09-17.json`, plus a `latest.json` pointer. Saving as plain
files is deliberate: it makes the data easy to diff day-over-day and requires no
database.

A neat trick: bye weeks aren't directly exposed by ESPN, so the tool infers them.
A player who is projected for zero points while otherwise healthy is on a bye.

## 4. Finding what changed (diffing)

Alerting on everything would be noise. So each run compares today's snapshot to the
most recent previous one and emits only real changes:

- Injury status changes (and it shouts louder when it's one of my starters).
- Players added or dropped anywhere I'm watching.
- A starter newly on a bye.
- A free agent that's suddenly trending up in ownership.

Because the previous snapshot is committed back to the project each day, "yesterday"
is always available, with no external storage.

## 5. The analysis: start/sit, waivers, game script

These checks look at today's snapshot alone (no history needed) and produce flags.

**Start/sit.** For each bench player, the tool looks for a starter it could
legally replace and is projected to beat. "Legally" matters: the tool knows each
starter's exact lineup slot, so it will suggest a wide receiver for a flex slot but
never for a required tight-end slot. It only flags when the bench player is
projected to beat the starter by a set margin (2 points by default), which focuses
attention on decisions that actually matter. On close calls, it adds a tie-breaker
from the Vegas game environment: if the bench player's team is expected to score
noticeably more than the starter's team, it says so.

**Waiver value.** Free agents are ranked by a value score designed so positions
compare fairly. The core of the score is **value over replacement**: a player's
projection minus what a freely available player at the same position would give
you. On top of that, it adds a bonus for a high Monte Carlo ceiling (upside), a
bonus for hot recent form, and a bonus if the player fills a position I'm weak at.
The top few per league are surfaced.

**Game script.** Using live point spreads, the tool flags players in lopsided
games: running backs on heavy favorites (likely to run out the clock), and
pass-catchers on heavy underdogs (likely to get garbage-time volume), for example.

## 6. The machine learning, explained

Four models, all learned from years of public NFL history and all fail-safe.

### Monte Carlo floor and ceiling

A single projected number hides risk. The tool turns each projection into a range:

1. From history, it learns each position's **performance-multiplier distribution**,
   which is simply each week's actual points divided by that player's season
   average. This is a dimensionless "boom or bust" shape: wide receivers swing
   harder than running backs, for instance. It's also split by scoring tier,
   because lower-scored players are relatively boomier than stars.
2. For a player projected for a given number of points, it simulates thousands of
   outcomes by multiplying the projection by randomly drawn multipliers, then reads
   off the 10th, 50th, and 90th percentiles as the floor, median, and ceiling. It
   also labels the shape as safe-floor, balanced, or boom-bust.

The clever part: because the outcome is projection times multiplier, the floor and
ceiling can be read directly from the multiplier percentiles, which makes it fast
and exact.

### Value over replacement

The tool computes, per position, the weekly points a "replacement level" player
scores. It does this by finding the last startable rank at each position for the
league's specific settings (teams times starters, splitting flex demand across
running back, receiver, and tight end), then measuring the historical points a
player at that rank scores. Subtracting this replacement level makes points
comparable across positions, which is exactly what waiver and trade decisions need.
Superflex leagues automatically raise the quarterback bar, since startable
quarterbacks are scarcer there.

### Heating up

Rather than guess what counts as a "notable" hot streak, the tool learns it. For
each position it computes the 75th percentile of historical positive jumps in
recent-form-versus-season-average, and uses that as the bar. A current player whose
recent three-week scoring clears their own season average by more than that bar is
flagged as heating up. History and the current season are independent sources that
share one unit (fantasy points), so a gap in the historical feed never blocks the
current-season signal.

### Game script from odds

The odds feed provides point spreads and over/unders. From those, the tool computes
each team's **implied point total**, how many points Vegas expects them to score,
as (game total minus the team's spread) divided by two. That number feeds the
game-script flags and the start/sit tie-breaker.

## 7. Proving it works (backtesting and model quality)

The most important part: I measured the tool instead of just claiming it was smart.
This lives in the `eval` folder and runs offline on historical data.

**The golden rule is no leakage.** When the tool grades a decision for a given week,
it may only use data from earlier weeks. This is enforced by shifting every rolling
average back by one week, and never letting data bleed across seasons. Without this
rule, a backtest would cheat by peeking at the future and look better than reality.

**The backtests** (in `eval/backtest.py`) replay the logic across five recent
seasons, about 26,000 player-weeks:

- **Start/sit.** For every pair of comparable players in a week, when the model
  projects one over the other by the margin, did that player actually score more?
  Across about 99,000 such calls, the answer was yes 63% of the time, versus 50%
  for a coin flip, adding about 3.7 points. Accuracy rises with the projected gap,
  reaching about 73% on the most confident calls.
- **Waiver value.** Flag the best waiver-pool players using data through a given
  week, then measure the next three weeks. Flagged players became startable 40% of
  the time, versus a 17% base rate, a 2.4 times lift.
- **Lineup capture.** The model's lineup captured 59% of the theoretically possible
  points, beating a naive "chase last week's scorers" strategy by 7 points.

**Model quality** (in `eval/model_quality.py`):

- **Projection error** was about 19% lower than a naive last-week baseline.
- **Calibration** checks whether the floor/ceiling ranges are honest. A raw 80%
  range only covered about 74% of real outcomes, meaning it was a touch too narrow
  (it ignored the projection's own uncertainty). After widening it, fit on some
  seasons and tested on a held-out season, it covered about 81%, almost exactly the
  80% it promises. That is a genuine, out-of-sample calibration result.

You can run both yourself: `python -m ffmonitor.eval.backtest` and
`python -m ffmonitor.eval.model_quality`.

## 8. Grading itself in real time (live outcomes)

Backtests are historical. To have a real, current-season record, the tool logs
every start/sit call it makes into `data/recommendations.json`, then grades each
one once that week's games are final by reading the players' actual scores from the
committed snapshots. A call is correct if the player it said to start outscored the
one it said to sit. The running record ("start/sit: 12-5, 71%") is shown in the
alert footer and available on demand with `python -m ffmonitor.outcomes`.

A subtle but crucial fix here: the workflow had to be told to commit the
recommendations file back to the repo, otherwise the log would reset every run and
never accumulate.

## 9. Sending alerts

Alerts go to Discord (rich cards) and optionally ntfy (plain phone push). The tool
stays quiet on days with nothing worth flagging, except for one daily "all clear"
digest so I know it actually ran.

A real bug surfaced here and is worth calling out: one busy morning, the alert came
through on ntfy but not Discord. The cause was that Discord rejects any single
message longer than 6,000 characters, and a packed "keep everything" digest
exceeded it. The fix splits alerts across multiple messages that each stay within
Discord's limits, splits a single oversized league across continuation cards, and
retries temporary failures. Now a big day simply becomes two messages instead of a
silent drop.

## 10. The AI assistant (Discord bot)

I wanted to ask questions from my phone. The catch: the Discord webhook that sends
my alerts is one-way, it can't receive questions. To type a question in Discord and
get an answer, Discord needs to reach an always-on service of mine.

The solution is a small serverless function (a Cloudflare Worker, free tier) that
Discord calls when I use a slash command. It does four things:

1. **Verifies the request** really came from Discord using a cryptographic
   signature check, with no external libraries.
2. **Answers Discord's validation ping** so the endpoint can be registered.
3. **Defers** its reply (Discord requires a response within three seconds, and
   calling an AI model takes longer), then does the real work in the background.
4. **Fetches my latest league data** from the repo and answers back in the channel.

There are three commands:

- **`/ask`** sends my question, plus a compact summary of my rosters and
  projections, to an AI model through OpenRouter, and posts the answer.
- **`/ranges`** lists my whole team's floor/median/ceiling for the week, computed
  directly from the snapshot with no AI, so it can never drop a player or invent a
  number.
- **`/trade`** compares two players (see the next section).

The AI model is swappable in a single command via `set-model.sh`, which edits the
config and redeploys.

## 11. Trade evaluation

Judging a trade requires knowing about players on other people's teams, which the
daily monitor didn't originally store. So the snapshot was extended to save every
team's roster (this data was already being read to filter free agents, so it cost
nothing extra), and the Monte Carlo ranges were extended to cover all of those
players too.

The `/trade give: X get: Y` command then finds both players anywhere in the league,
computes each one's value over replacement and floor/ceiling range, and gives a
verdict on which side wins and by how much, plus a short AI take on the fit. It is
honest about its limits: it judges value on paper and can't read the other
manager's mind.

## 12. Engineering practices

- **Fail-safe everywhere.** Machine learning, odds, and the assistant each degrade
  to a no-op if their data or dependency is missing, so the core monitor never
  breaks.
- **Secrets stay secret.** Every cookie, token, and key lives in encrypted GitHub
  or Cloudflare secrets, never in the code.
- **Tests and continuous integration.** A unit-test suite covers the tricky logic
  (lineup-slot eligibility, the Discord message splitting, the outcome grading,
  config parsing, and the leakage-safe feature prep) and runs automatically on
  every push.
- **No infrastructure to babysit.** GitHub Actions schedules the runs and stores
  the data, and Cloudflare hosts the bot, all on free tiers.

## 13. Problems we hit and how we fixed them

- **ESPN cookies and multi-league setup.** Getting private-league access working
  meant extracting account cookies from the browser; one pair covers all leagues
  since they're tied to the account, not the league.
- **Historical data gaps.** The NFL history source doesn't publish the current
  season right away, which caused errors. Fixed by fetching season-by-season and
  skipping any that aren't available yet.
- **Discord message-size limit.** Described above: split into multiple messages.
- **Illegal start/sit suggestion.** The tool once suggested benching my only tight
  end for a wide receiver. Fixed by teaching it each starter's exact lineup slot and
  who can legally fill it.
- **The scheduler's delivery time.** Scheduled runs can be delayed, and the top of
  the hour is the most congested slot, so the daily run was moved off the hour to
  land closer to the target time.
- **Trade data not present.** The bot couldn't see other teams until the snapshot
  was extended to store all rosters and the monitor was re-run to refresh the data.

## 14. Honest limitations

- The backtest validates the decision logic plus a reconstructed projection, not
  ESPN's live projections, so it likely understates the live tool. Results are
  always described as backtested, not guaranteed.
- Football is high-variance, so no tool can call two closely matched players
  reliably; that ceiling is real.
- The trade evaluator judges paper value only.
- The richest current-season usage signals aren't on free data feeds, which caps
  how far the models can go without paying for data.

## 15. Glossary

- **Snapshot:** a saved picture of a league on a given day.
- **Projection:** an estimate of how many points a player will score this week.
- **Floor / median / ceiling:** a bad-case, typical, and great-case outcome.
- **Value over replacement:** a player's value above a freely available player at
  the same position, which lets positions be compared fairly.
- **Monte Carlo simulation:** estimating a range of outcomes by running many random
  trials.
- **Calibration:** whether a predicted range is honest (an 80% range should contain
  the real result about 80% of the time).
- **Leakage:** accidentally using future information to judge a past decision, which
  makes a model look better than it is; avoided here on purpose.
- **Backtest:** replaying the logic on historical data to measure how well it would
  have done.
- **Implied team total:** how many points Vegas expects a team to score, derived
  from the betting line.
