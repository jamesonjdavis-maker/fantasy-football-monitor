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
never for a required tight-end slot. The projection it compares on is not ESPN's raw
weekly number but the layered-model median (the Monte Carlo center, which reflects
defense, Vegas, weather, and the ESPN/form blend), so the comparison uses the best
estimate available rather than the raw feed. It only flags when the bench player is
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

The projection the range is built around is not ESPN's raw weekly number but the
tool's own **layered current-week projection** (the one described under "the layered
model" below, which already folds in defense-vs-position, Vegas, weather, and the
ESPN/form blend). Because the simulation scales the spread proportionally to that
center, using the sharper center sharpens the whole range without changing its
boom/bust shape, so the floor, median, and ceiling everything downstream relies on
(start/sit range notes, the trade range shown on the card, and the risk blend in
trade value) all reflect the full model rather than a raw projection.

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

The odds come from each game's summary endpoint on ESPN's public API (its
`pickcenter`), which is where ESPN moved the betting lines after dropping them from
the scoreboard feed. The lines power both the start/sit tie-breaker and the Vegas
layer of the projection model below.

### Rest-of-season weekly projections (the layered model)

A trade or a long-term roster call is about what a player is worth from here to the
end of the season, not this week. The problem: ESPN's API only exposes a player's
current-week projection and a season average, never a real week-by-week forecast
for the rest of the year (I confirmed this by requesting future scoring periods
directly, which just return the season average). So the tool computes its own
rest-of-season line for every remaining week.

It is a **layered model**: a per-game baseline, multiplied by a set of clamped
factors, with each layer capturing one thing and only one thing, so nothing is
double-counted. The code lives in `ffmonitor/ml/matchup.py` (the per-week factors),
`ffmonitor/ml/playerfactors.py` (the player and team season factors), and
`ffmonitor/ml/weather.py` (the weather factor), and the result is attached to every
player in the snapshot as `ros_proj` by `ffmonitor/platforms/espn.py`.

**The baseline** is a per-game scoring level for the rest of the season. It blends
ESPN's forward-looking projected average (75%) with the player's own realized form
(25%). ESPN's projected average is the sharper anchor because their analysts fold
in role, depth-chart, and injury news that free data can't see, so it doesn't lag a
slow start the way a raw season average does (a back returning from injury is
projected near his real level, not the handful of quiet games he has actually
played). The 25% realized-form term pulls a big gap between projection and reality
partway back toward what is actually happening on the field, in both directions: a
player ESPN projects well above his recent play gets tempered down, and one
outscoring his projection gets nudged up, without gutting a genuine bounce-back.
This blend is the one place the overall *level* leans on ESPN; everything else
below, the week-to-week movement, is computed here. The two blend weights live in a
`PROJECTION TUNING` block at the top of `ffmonitor/platforms/espn.py`.

**The level factors** (roughly constant across weeks, so they scale the baseline)
come from the NFL history data:

- **Volume / role trend:** recent opportunity (targets plus carries, or pass
  attempts for QBs) versus the season, so a rising role nudges up and a shrinking
  one down. This is forward-looking in a way raw points are not, because role leads
  production.
- **Efficiency regression:** a player's points per opportunity pulled part of the
  way back toward the norm for their position, so a touchdown-heavy hot start does
  not project forward as if it will continue.
- **Team pace and pass rate:** a fast or pass-heavy offense lifts its pass catchers
  (and a run-heavy one its backs), measured as the team's plays and pass rate
  versus the league.

**The weekly factors** (what actually makes each week differ) are:

- **Opponent, defense vs. position:** how many fantasy points that week's opponent
  allows to the player's position per game, divided by the league average, clamped
  so no single matchup is absurd.
- **Vegas game environment:** the team's implied total (more expected scoring lifts
  everyone) and a position-specific game-script nudge from the spread (favorites
  help their backs, underdogs help their pass catchers). Near-term only, since
  books do not post lines months out.
- **Weather:** wind, precipitation, and extreme cold at outdoor stadiums cut into
  the passing game, pass catchers more than backs, using the free Open-Meteo
  forecast for games inside its window. Domes and far-out games are untouched.
- **Injury:** a known OUT, IR, PUP, or suspension zeroes the current week; doubtful
  and questionable take a haircut. Status only speaks to the current week, so that
  is the only week it touches.
- **Bye weeks** are set to zero.

Two honesty points matter. First, these are clearly **labeled as the tool's own
estimates**, not ESPN's numbers. Second, the far-out weeks necessarily lean mostly
on the baseline and the opponent, because Vegas lines, weather forecasts, and
injury statuses only exist for the near term; that information folds in on its own
as each week approaches.

**A data-availability wrinkle:** the matchup and player/team factors need recent
weekly NFL stats, and nflverse does not always have the current (or even the most
recent) season published yet. So the tool walks back to the most recent season
whose weekly data actually loads and uses that as the prior. Last season's
defensive tendencies are a sound starting point before the new season has enough
games, which is the same thing real projection systems do early in a year. A small
`proj_meta` block is written into each snapshot recording which data layers loaded
and which season the matchup data came from, so a flat or missing layer is easy to
diagnose instead of silently wrong.

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

### A fair question: how can you backtest a model built from history?

If a model learns from historical data, testing it on historical data sounds
circular. The resolution is the foundational rule of machine-learning evaluation:
split history into a training slice and a testing slice, and never let the test
slice influence the model.

For the calibration specifically, the tool learns each position's volatility from
all seasons except the most recent, then measures whether its ranges hold up on the
most recent season, which it never saw. Right now that means training on 2020
through 2023 and testing on 2024. Both are history, but they are separate,
non-overlapping slices, so it is a genuine out-of-sample test.

It is worth being precise about which parts this applies to. The start/sit and
waiver backtests do not learn parameters from the data; they apply fixed,
leakage-safe rules and grade each week against that week's own future, so they do
not need a train/test split. The split matters specifically for the calibration,
which fits a range-width factor and has to prove it generalizes.

### Why it keeps working as new seasons arrive

When next season's data (2025) is published, the split shifts automatically: the
tool trains on 2020 through 2024 and tests on 2025. Moving 2024 into the training
set is correct, not a problem. The only rule is that the season being tested stays
out of training, and 2025 is now the untouched test. The model "knowing about" 2024
tells it stable patterns, like how boom-or-bust each position tends to be, but it
says nothing about who scores what in 2025, so it cannot leak. Using older seasons
to forecast a newer one is exactly what you want.

This retrain-on-the-past, test-on-the-newest-season approach is a standard technique
called walk-forward (or expanding-window) validation. It holds up year over year
because the patterns the model relies on, position volatility and replacement
levels, are stable across seasons, and the live current-season signals come from a
separate source (ESPN) anyway. The only thing that would undermine it is a dramatic
change to the game's scoring or rules that makes old seasons unlike new ones, which
has not been an issue.

### Keeping the start/sit result honest too

The start/sit projection has one tunable knob: how much to weight recent form versus
the season average. Because I chose that weight by comparing options on the data,
reporting accuracy on the same seasons would be a mild case of grading a setting on
its own homework. To rule that out, I re-ran it the strict way: I selected the blend
using only 2020 through 2023, then measured start/sit accuracy on a held-out 2024
that the choice never saw. The out-of-sample result was 63.2% (about 19,300 graded
calls), essentially identical to the all-seasons figure. Two things make this
reassuring: the blend is a single, insensitive parameter (every setting landed within
about a point of the others), so there was little room to overfit, and the honest
out-of-sample number confirms the headline result was not inflated by fitting.

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

There are five commands:

- **`/ask`** sends my question, plus a compact summary of my rosters and
  projections, to an AI model through OpenRouter, and posts the answer.
- **`/ranges`** lists my whole team's floor/median/ceiling for the week, computed
  directly from the snapshot with no AI, so it can never drop a player or invent a
  number.
- **`/trade`** evaluates a trade, with multiple players per side, and grades each
  team on how its actual starting lineup changes week by week (see the next section).
- **`/projections`** prints a player's week-by-week projected points for the rest
  of the season, straight from the layered model, with byes flagged and a remaining
  total.
- **`/tradefinder`** takes a team to trade with and suggests deals that help both
  sides (see the next section).

Every player and team field uses Discord's **autocomplete**, so the options come
from a live dropdown of my actual rosters and league teams. Because a completed
pick replaces the whole field, the multi-player trade inputs use numbered slots
(`give1`, `give2`, and so on) rather than one comma-separated box, which is what
autocomplete supports cleanly. The AI model is swappable in a single command via
`set-model.sh`, which edits the config and redeploys.

## 11. Trade evaluation

Judging a trade requires knowing about players on other people's teams, which the
daily monitor didn't originally store. So the snapshot was extended to save every
team's roster (this data was already being read to filter free agents, so it cost
nothing extra), and the Monte Carlo ranges were extended to cover all of those
players too.

The `/trade` command finds the players anywhere in the league, computes each one's
value over replacement, and gives a verdict on which side wins and by how much,
plus a short AI take on the fit. The value is now based on the **rest-of-season
projection** from the layered model (per-game projected points over a
replacement-level player at that position), not a single week, because a trade is a
rest-of-season decision. Replacement level is computed on the same rest-of-season
basis from the free-agent pool, so the comparison stays apples-to-apples.

Several things were added on top of the original one-for-one version:

- **Multiple players per side.** Each side takes up to three players (`give1..give3`,
  `get1..get3`), so 2-for-1 and 3-for-2 deals work. The card labels each side clearly
  ("you send away" / "you receive", or the two team names) and shows a subtotal under
  each so it's obvious which players the number refers to. Each player's line also shows
  his weekly floor-to-ceiling range in brackets, for example `[7.9-30.5]`, so the spread
  the value and grade blend in (the risk blend above) is visible at a glance.
- **Consulting on other managers' trades.** Optional `team_give` and `team_get`
  fields name the two teams involved, so a trade between two other managers is
  judged neutrally by team name instead of assuming one side is mine, and both teams'
  rosters are used to grade it.
- **A trade finder.** `/tradefinder` takes a partner team and proposes deals that
  help both sides. It measures each team's weak spots (a thin starter at a position)
  and its surplus depth, then pairs my depth at their weak spot against their depth
  at mine, within a fair value band, and ranks by how much each lineup improves. It
  proposes multi-player packages as well as one-for-ones, and the AI picks the best
  couple to actually pitch. See "The finder is not the analyzer" below for how it
  differs from `/trade`.

### Grading a trade by what you actually start

The interesting part is how a trade is graded. The obvious measure, total value sent
versus received, is misleading for two reasons. First, it is zero-sum: whatever value
one side gains, the other loses by the same amount, so the two grades would always be
mirror images. Second, and more important, you only start a fixed lineup, so a bench
player you would never play is worth almost nothing to your weekly score, and trading
three role players for one star starter can be a clear win even though the raw point
totals say you came out behind.

So each team is graded on the change to the lineup it would actually start, computed
week by week:

- For every remaining week, the tool rebuilds that team's best legal starting lineup
  (QB, RB, WR, TE, and FLEX, from the league's real settings) using that week's
  projection, and sums the starters.
- It does this for the roster before the trade and after it, and takes the difference.
- A player on a bye or injured that week counts as zero, so he can't prop up the
  lineup in a week he wouldn't play. Depth that never cracks the starting lineup
  barely moves the grade; landing one player who bumps a weak starter moves it a lot.

The grade is the average of those weekly changes, on a plain A-to-F scale, and the
card also reports the total points gained or lost over the rest of the season and
flags the roughest single week (usually a bye or injury collision). Because each team
optimizes its own lineup, the grades are **not** mirror images: a smart deal where
each side trades from a strength into a need can grade well for both, while a lopsided
one shows the real gap.

On top of that starting-lineup number, the grade folds in a small weight for bench
depth, because the lineup you start today is not the only thing that matters: injuries
and byes mean you want enough quality bodies behind your starters. Depth is measured as
the change in the value of the players a team would **not** start (its full roster value
minus its optimal starting value). Gaining a useful extra body, as in a two-for-three
deal, earns a small bonus; consolidating several players into one star, as in a
three-for-two, takes a small ding for the roster spot and insurance it costs. This depth
effect is deliberately minor and is **clamped to plus or minus one point per week**, so
it can only nudge a grade that sits near a boundary; it can never outweigh the starting
lineup, which is still what mostly decides the trade. The card shows it alongside the
starters line, for example `starters +1.2 pts/wk (+8.4 over 7 wk, depth +0.8)`. The two
constants that control it, the depth weight and the cap, live at the top of the trade
logic and are easy to retune.

The underlying value still uses the rest-of-season projection from the layered model
(per-game projected points over a replacement-level player at that position, with
replacement computed on the same basis from the free-agent pool). Because that
baseline leans mostly on ESPN's forward-looking projected average (blended with the
player's realized form), it already reflects a player's expected role and injuries,
so a player riding a temporary spike (for example, a receiver producing only because
a teammate is hurt) is valued on what he is expected to do going forward, not his hot
streak, while a big gap between projection and recent play is tempered partway toward
reality. A known multi-week absence (IR, PUP, a suspension) zeroes out the weeks the
player is expected to miss.

That per-game value is not just the mean, and it is not just the floor or the ceiling:
it is a risk-adjusted blend of all three. The mean (which, as above, already blends ESPN's
projection, realized form, defense, Vegas, weather and injuries) is the base, and the
shape of the player's Monte Carlo range then nudges it. Upside above the median adds value
and downside below the median subtracts it, each measured as a ratio to the median so the
nudge is scale-independent and reacts to how volatile a player is rather than his raw
score. A steady player with a tight range stays right at his mean; a boom player whose
ceiling towers over his median gains; a bust-risk player whose floor sits well below it
loses. Because fantasy scoring is right-skewed (ceilings run further above the median than
floors run below), most players get a small net boost, but a safe high-floor player and a
volatile low-floor player with the same mean no longer look identical, which was the whole
point. The two weights (one for upside, one for downside) and the cap that keeps the whole
adjustment within a modest band are three named constants in the config block, so the
blend is easy to retune, and setting both weights to zero falls back to the pure mean.

Because replacement level is computed on this same blended basis, value over replacement
stays apples-to-apples. The same factor is applied everywhere a trade values a player: the
value shown on the card, the finder's rankings, and the week-by-week starting-lineup grade
itself, where each week's projection is scaled by the player's blend factor before the
optimal lineup is rebuilt. Because the grade is a difference between the lineup before and
after the trade, the shared part cancels and what remains is the relative edge the range
gives, so acquiring boom potential or safe production helps the grade a little and shipping
it away costs a little.

### The plain-English summary and the AI take

Below the graded card, a follow-up message explains the deal in words. It leads with a
**summary written entirely by code** from the computed numbers: for each team, what it
adds, what it gives up, its grade, and its lineup change, for example "Staff infection
upgrades its starting lineup, B (+1.2 starting pts/wk, +0.8 depth). Adds Rice, Odunze,
and Skattebo; gives up Adams and Dowdle." Because these sentences are assembled straight
from the data rather than generated, they **cannot** flip who gets whom, call a player a
team receives a loss, or contradict the grades. This is the source of truth.

Underneath that, an AI model adds a short plain-English take for color. The deterministic
card and summary never depend on the model, so a weak or cheap model can only affect the
flavor text, not the numbers or the factual summary above it. Two guardrails keep the AI
prose honest as well: it is fed each team's own additions and losses with its grade, told
to treat a received player as a gain and never attribute a player to the wrong team, and
the code checks the result and re-asks if a player was dropped. The summary and take are
sent as their own follow-up message so a long card and a long write-up never share a
single message's length limit and get cut off.

It stays honest about its limits: it grades the lineup you would optimally start, not
the one you actually set each week, and it can't read the other manager's mind.

### The finder is not the analyzer

`/trade` and `/tradefinder` look similar because they share the same projections,
replacement levels, and value math, but they answer different questions, and they
should not behave the same way.

`/trade` is an **analyzer**. You already have a specific deal in mind, and it grades
that deal: it tells you whether this exact set of players going each way helps or hurts
each team's starting lineup. It scores whatever you hand it, including an upgrade at a
position you are already strong at, because that is a real (if minor) effect of the
trade you asked about.

`/tradefinder` is a **proposer**. Nobody asked about a specific deal; it has to invent
ones worth pitching. If it scored every upgrade the way the analyzer does, it would keep
offering you a slightly better quarterback when your quarterback is already fine, which
is useless advice ("why are you telling me to trade for a QB? I don't need a QB"). So the
finder is deliberately **need-first**. It finds each team's weak spots, defined as a
position where the weakest player it would have to start is no better than a freely
available waiver player (its value over replacement is at or below zero) or the slot is
empty. That replacement-relative bar matters: a deep, streamable position like quarterback
has a high replacement level, so a perfectly good starting QB still shows a low value over
replacement, and a flat points bar would wrongly flag it as a need. It is not one; in a
one-quarterback league you start your QB and stream on bye or injury weeks rather than
trading for another. An upgrade that lands at a true weak spot counts in full, while an
upgrade at a position the team is already set at counts at a steep discount (currently one
fifth). The result is that genuine need-fills
always rank well above marginal upgrades, so the finder stops suggesting a position you
do not need and leads with deals that fix an actual hole on both sides. The bar and the
discount are two named constants in the `TRADE_TUNING` block (see "Where the tunable
weights live" below), easy to retune.

In short: `/trade` judges a deal you bring; `/tradefinder` goes looking for deals that
fix needs. Use the finder to generate ideas, then drop a specific one into `/trade` for
the full week-by-week, depth-weighted grade.

### Where the tunable weights live

Every judgment call the trade logic makes is gathered into one labeled block, so there
is a single place to change how the tool thinks without hunting through the code. The
trade-decision weights sit in a `TRADE_TUNING` block at the top of
`discord-bot/worker.js`: the value blend (upside and downside weights and their cap), the
bench-depth weight and its cap, the letter-grade cutoffs, and the trade finder's need bar,
set-position discount, minimum lineup gain, fair-value band, and ranking penalties. The one projection-side weight, how
much the per-game baseline trusts ESPN's projection versus the player's realized form,
sits in a matching `PROJECTION TUNING` block at the top of
`ffmonitor/platforms/espn.py`. Everything outside those two blocks (defense-vs-position,
Vegas, weather, level factors, injury discounts) is a measured input rather than an
opinion, so it is not something you would tune by hand.

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
- **No future-week projections from ESPN.** The API only returns the current week's
  projection and a season average, which first showed up as flat, identical weekly
  numbers. The fix was to stop asking ESPN for future weeks and build the layered
  model instead.
- **nflverse 404s on the newest seasons.** The weekly-stats source returned a 404
  for the current and most recent season, which quietly emptied every matchup and
  usage factor and flattened the projections. Fixed by walking back to the most
  recent season that loads, with a `proj_meta` diagnostic in the snapshot so an
  empty layer is visible rather than silent.
- **ESPN moved the betting lines.** Odds disappeared from the scoreboard feed, so
  the odds signal silently loaded zero teams. Fixed by reading each game's line from
  its summary `pickcenter` endpoint instead.

## 14. Honest limitations

- The backtest validates the decision logic plus a reconstructed projection, not
  ESPN's live projections, so it likely understates the live tool. Results are
  always described as backtested, not guaranteed.
- Football is high-variance, so no tool can call two closely matched players
  reliably; that ceiling is real.
- The trade evaluator judges paper value only.
- The rest-of-season weekly projections are the tool's own estimates, not ESPN's.
  ESPN does not expose a real future-week forecast, so the tool models one; the
  far-out weeks in particular lean on the baseline and opponent because Vegas,
  weather, and injury information only exist for the near term.
- The matchup and usage factors depend on recent weekly NFL stats, which the free
  data source does not always have for the current or most recent season. When that
  happens the tool falls back to the most recent season that is published, so early
  in a year those factors reflect last season's tendencies rather than this one.
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
