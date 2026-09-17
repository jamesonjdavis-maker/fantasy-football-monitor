# Fantasy Football Monitor: Project Report

A plain-English walkthrough of what I built, why, how it works, and how I know it
works. Written so I can hand it to anyone, technical or not, and have them follow
along.

---

## 1. The problem

I play in five ESPN fantasy football leagues. Staying on top of all of them meant
logging into each one every morning to check for injuries, waiver-wire activity,
bye weeks, and whether I had a better option sitting on my bench. It was ten
minutes of tedious clicking a day, and I still missed things.

So I built a tool that does it for me: one program that checks all five leagues
every morning, figures out what actually changed, runs some analysis to find real
opportunities, and sends me a single message, only when something is worth
knowing. It runs itself in the cloud on a schedule, and I can ask it questions
from my phone.

What started as a chore-saver turned into a project about data pipelines, machine
learning, and honest measurement.

## 2. What it does

- **Watches all five leagues** and pulls my roster, bench, and the top free agents
  from each one every day.
- **Notices real changes** by saving a daily snapshot and comparing it to
  yesterday's, so it only surfaces things that moved: an injury flipping to OUT, a
  player added or dropped, a starter on a bye, a free agent suddenly getting
  picked up everywhere.
- **Gives start/sit and waiver advice.** It flags bench players projected to beat
  a starter (and it respects lineup rules, so it never tells me to bench my only
  tight end for a wide receiver). On close calls it breaks the tie with the Vegas
  game environment. It ranks the best free agents by a value score, and reads live
  betting lines to spot favorable game scripts.
- **Keeps its own report card.** It logs every start/sit call it makes and grades
  it once the games are final, so I have a running, real-world accuracy record.
- **Answers questions and sizes up trades** from Discord on my phone, using my
  actual rosters and projections, including every other manager's roster.
- **Only bothers me when it matters,** and stays quiet otherwise.

## 3. How it works (the pipeline)

Every run does the same simple loop:

1. **Collect.** Pull the current state of each league from ESPN.
2. **Snapshot.** Save it as a dated file, and compare against yesterday's file to
   produce a list of real changes.
3. **Analyze.** Run the start/sit, waiver, game-script, and machine-learning
   checks against today's data.
4. **Grade.** Score any past recommendations whose games have now finished.
5. **Alert.** If anything is worth flagging (or it's the daily digest), send it to
   Discord.

The whole thing runs on **GitHub Actions**, a free scheduler, and commits each
day's snapshot back into the project. That saved history is what the next day
compares against, so there is no database or server to run or pay for. Every
password and cookie lives in GitHub's encrypted secrets, never in the code.

## 4. The data

Everything runs on free, public data from three sources:

- **ESPN's fantasy API** for my live rosters and this week's projections.
- **ESPN's public odds feed** for point spreads and over/unders (the Vegas
  signal).
- **nflverse**, a public dataset of historical NFL stats, for everything the
  machine-learning models learn from.

An important design point: the historical data and the current-season data are
independent sources that happen to share one unit, fantasy points. That means a
gap in one (nflverse doesn't publish the current season until it's underway)
never blocks the other.

## 5. The analytics

This is the interesting part. Four ideas, each turning a hand-picked guess into
something learned from data.

- **Monte Carlo floor and ceiling.** A single projected number hides risk: 12
  points could be a safe 10 to 14, or a boom-or-bust 3 to 28. So I learned each
  position's week-to-week volatility from years of history, then simulate thousands
  of possible outcomes for each player to produce a floor, a median, and a ceiling.
- **Value over replacement.** Twelve points at quarterback is not worth the same as
  twelve at tight end, because a startable quarterback is easy to find and a
  startable tight end is not. So a player's value is measured against what a freely
  available player at that position would give you. That makes players comparable
  across positions, which is exactly what waiver and trade decisions need.
- **"Heating up" detection.** Using years of history, I worked out how big a jump
  in recent production actually counts as notable for each position, then flag
  players whose recent form clears that bar.
- **Game script from betting lines.** Heavy favorites tend to run the ball late,
  which helps their running backs; heavy underdogs throw to catch up, which helps
  their pass-catchers. The tool reads live spreads and each team's Vegas-implied
  point total and factors it in.

## 6. Does it actually work?

I didn't want to just claim the tool was smart, so I measured it. I built a
backtest that replays the recommendation logic across five recent NFL seasons
(about 26,000 player-weeks), using only the information that would have been
available at the time, with no peeking at the future.

- **Start/sit calls were right 63% of the time** across about 99,000 head-to-head
  calls, versus 50% for a coin flip, adding roughly 3.7 points to the better
  choice. Accuracy climbs as the projected gap grows: on the most confident calls
  (an 8-plus point gap) it reaches about 73%.
- **Flagged waiver pickups were 2.4 times as likely** to become startable over the
  next three weeks as a typical available player (40% versus 17%).
- **Lineup choices captured 59% of the theoretically possible points,** beating a
  naive "start last week's top scorers" strategy by 7 points.
- **The floor/ceiling ranges are calibrated.** After tuning, real outcomes land
  inside the predicted 80% range about 81% of the time, tested on a season the
  model had never seen. A range you can trust is what makes the safe-versus-boom
  labels meaningful.
- **Projections beat a naive baseline** by about 19% on average error.

On top of the backtest, the tool now grades its own calls live during the season,
so the accuracy record is real, not just historical.

## 7. The AI assistant

I can ask the tool questions, and check trades, from Discord on my phone. To do
that I built my own Discord bot: a Discord application with a few slash commands,
running on a free Cloudflare Worker (a small cloud function). It verifies each
request really came from Discord, pulls in my latest league data, and answers back
in the channel:

- **`/ask`** answers a natural-language question with an AI model, using my league
  data as context.
- **`/ranges`** lists my whole team's floor, median, and ceiling for the week,
  straight from the Monte Carlo model, with no AI involved so it can't drop a
  player or make up a number.
- **`/trade give: … get: …`** compares two players' value over replacement and
  their floor/ceiling ranges, across every team's roster in the league, and gives a
  verdict plus a short AI take.

The AI model is swappable in one command, and the same answering logic is also
available as a plain terminal command.

## 8. Engineering practices

- **Fail-safe by design.** Every advanced feature (machine learning, odds, the
  assistant) degrades to a no-op if its data or dependency is unavailable, so the
  core monitor never breaks.
- **Secrets stay secret.** All credentials live in encrypted GitHub and Cloudflare
  secrets, never in the code.
- **Tested and automated.** A unit-test suite covers the core logic and runs
  automatically on every push through a continuous-integration workflow.
- **No infrastructure to babysit.** The scheduler, storage, and hosting are all
  free tiers, and the daily snapshot doubles as the database.

## 9. Honest limitations

- The backtest validates the decision logic plus a reconstructed projection, not
  ESPN's live projections (which aren't published for past seasons), so it likely
  understates the live tool. I always describe results as backtested, not
  guaranteed.
- Football is high-variance. Even a perfect projection can't call two closely
  matched players, so no tool gets start/sit accuracy near 100%.
- The trade evaluator judges value on paper. It can't read another manager's mind,
  so the human side of a deal is still my call.
- The richest usage signals (snap share, target share in the current season) aren't
  available on free feeds, which is the ceiling on how far the models can go
  without a paid data source.

## 10. What this project demonstrates

- Building a real, end-to-end data pipeline that runs itself and delivers value
  every day.
- Applying machine learning (simulation, calibration, value-over-replacement) to a
  genuine problem, and being honest about what it can and can't do.
- Rigorous, leakage-safe evaluation, and measuring a model instead of just
  asserting it works.
- Full-stack range: a Python backend, a JavaScript serverless function, cloud
  scheduling, API integrations, an AI assistant, tests, and CI.
- Judgment about scope: knowing which ideas were worth building and which were
  vanity, and saying so.
