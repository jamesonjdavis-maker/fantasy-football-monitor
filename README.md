# Fantasy Football Monitor

I play in five ESPN fantasy football leagues, and I got tired of logging into
each one every morning to check for injuries, waiver-wire movement, and whether
I had someone on my bench who should be starting. So I built a tool that does it
for me.

Every morning it checks all five leagues, notices what actually changed since
yesterday, runs some analysis to spot start/sit and waiver opportunities, and
sends me a single Discord message, but only when there's something worth
knowing. It runs itself on a schedule in the cloud, and I can even ask it
questions from my phone.

This started as a way to save myself ten minutes a day. It turned into a project
about data pipelines, machine learning, and honest model evaluation.

## What it does

- **Watches all my leagues at once.** It pulls my roster, bench, and the top
  free agents from each ESPN league every day.
- **Notices real changes.** It saves a snapshot each day and compares it to
  yesterday's, so it only tells me about things that actually moved, like an
  injury designation flipping to OUT, a player getting added or dropped, a starter
  heading into a bye week, or a free agent that's suddenly getting picked up
  everywhere.
- **Gives me start/sit and waiver advice.** It flags bench players projected to
  outscore a starter (and it knows the rules, so it won't tell me to bench my
  only tight end for a wide receiver), and on the close calls it breaks the tie
  with the Vegas game environment, how many points each team is expected to
  score. It also ranks the best free agents by a value score and reads live
  point spreads to spot favorable game scripts.
- **Only bothers me when it matters.** Quiet days get a short "all clear"
  digest; busy days get the details. Everything lands in Discord.
- **Keeps its own report card.** It logs every start/sit call it makes and grades
  it once the games are final, so I have a running record of how often it's right.
- **Answers questions.** I can ask it "who should I start in the BBL this week?"
  right from Discord and it replies using my actual rosters and projections.

Under the hood it keeps every password and cookie in GitHub's encrypted secrets,
never in the code, and runs on a free GitHub Actions schedule that commits each
day's snapshot back to the repo, so there's no database or server to pay for.

## The analytics

The fun part. All of this runs on free, public data: the ESPN API for live
projections and a public NFL history dataset (nflverse) for everything else.

- **Monte Carlo floor and ceiling.** A single projected number hides risk: 12
  points could be a safe 10–14 or a boom-or-bust 3–28. So instead of trusting
  one number, I learned each position's week-to-week volatility from years of
  history and simulate thousands of possible outcomes for every player, then read
  off a floor, a median, and a ceiling.
- **Value over replacement.** Twelve points at quarterback is not the same as
  twelve at tight end, because a startable quarterback is easy to find and a
  startable tight end isn't. So waiver value is measured against what a freely
  available player at that position would give you, which makes players
  comparable across positions.
- **"Heating up" detection.** Using years of history, I worked out how big a jump
  in recent production actually counts as notable for each position, then flag
  players whose recent form clears that bar.
- **Game script from betting lines.** Heavy favorites tend to run the ball late
  (good for their running backs); heavy underdogs throw to catch up (their
  pass-catchers get garbage-time volume). It pulls live point spreads and flags
  the players affected.

## Does it actually work?

I didn't want to just claim it was smart, so I wanted to measure it. I built a
backtest that replays the recommendation logic across five recent NFL seasons,
using only the information that would have been available at the time, with no
peeking at the future. A few of the results:

- **Its start/sit calls were right 63% of the time.** Whenever the model said to
  start one player over another, the player it picked really did outscore the
  other one 63% of the time, across about 99,000 of these head-to-head calls. With
  no information at all you would be right half the time (a coin flip), so 63% is a
  real edge, and each correct call added roughly 3.7 points to the lineup. Those
  points are what win close matchups.
- **The free agents it flagged panned out 2.4 times as often.** When it points at
  a player on the waiver wire as worth adding, 40% of those players went on to
  become genuinely start-worthy over the next three weeks, versus only 17% for a
  typical available player. In plain terms, its picks hit more than twice as often
  as grabbing someone at random.
- **Its floor and ceiling ranges can be trusted.** For each player it gives a
  range it expects the real result to fall inside 80% of the time. Tested against
  seasons it had never seen, the real result landed in that range about 81% of the
  time, almost exactly what it promised. A range you can trust is what makes the
  "safe" versus "boom-or-bust" labels actually mean something.

You can run the evaluation yourself:

```bash
python -m ffmonitor.eval.backtest        # start/sit, waiver, and lineup backtests
python -m ffmonitor.eval.model_quality   # projection error and calibration
```

And you can check the live, in-season record any time:

```bash
python -m ffmonitor.outcomes
```

## Ask it questions

There are two ways to ask, both powered by an OpenRouter model and grounded in
your live rosters and projections.

- **From your terminal:**
  ```bash
  python -m ffmonitor.ask "Start Odunze or Fannin in the BBL this week?"
  ```
- **From Discord, on your phone,** type `/ask` in your server and the answer
  comes back in the channel.

To make the Discord version, I created my own Discord bot: a Discord application
with an `/ask` slash command that anyone in the server can use. When you run the
command, Discord sends the question to a small serverless function I deployed on a
free Cloudflare Worker. That function checks the request really came from Discord,
pulls in my latest league data, asks the model, and posts the answer back into the
channel. Because it lives in the cloud, it works from my phone without my computer
being on. The full walkthrough for building your own is in
[discord-bot/SETUP.md](discord-bot/SETUP.md).

## Setup

### 1. Find your ESPN league info

For each league, grab the league ID and your team ID from the URL when you're
viewing your team, which looks like
fantasy.espn.com/football/team?leagueId=**1234567**&teamId=**3**.

The tool takes them all in one setting, ESPN_LEAGUES, as a comma-separated list
of Label:leagueId:teamId, for example:

```
Masters:1436101602:3, Big Beautiful League:576110229:3
```

The label is just what shows up in your alerts so you know which league each item
came from.

**Private leagues** also need two cookies from a browser where you're logged into
ESPN. In Chrome, sign in to espn.com, open DevTools → Application → Cookies →
espn.com, and copy the values of espn_s2 (a long string) and SWID (looks like
{XXXX-XXXX-...}, braces included). One pair of cookies covers all your leagues,
since they're tied to your account, not to a single league. Treat them like a
password. They go in your secrets, never in the code, and they expire every so
often, so refresh them if ESPN stops working.

### 2. Set up alerts

Create a Discord webhook (Server Settings → Integrations → Webhooks → New Webhook
→ Copy Webhook URL) and that's your alert channel. If you'd rather get plain
phone pushes, the tool also supports ntfy. Pick an unguessable topic name,
subscribe to it in the ntfy app, and use that name. You can set either or both.

### 3. Try it locally (optional)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
ALWAYS_NOTIFY=1 python -m ffmonitor.main
```

ALWAYS_NOTIFY=1 forces an alert even when nothing's flagged, so you can confirm
your setup works. With no alert channel configured, it just prints what it would
have sent.

### 4. Put it on GitHub

Push the project to a GitHub repo, then add your values under Settings → Secrets
and variables → Actions. The ones you'll want:

- ESPN_LEAGUES (your comma-separated league list)
- ESPN_S2 and ESPN_SWID (only for private leagues)
- DISCORD_WEBHOOK_URL (and/or NTFY_TOPIC)
- OPENROUTER_API_KEY (only if you want the ask feature)

The workflow runs every morning and an extra time on Sunday before games lock.
You can also trigger it by hand from the Actions tab, ticking the always_notify
box to test your webhook end to end. It has permission to commit each day's snapshot
back to the repo, and that saved history is what the next day's run compares
against, so there's no database to set up.

## Tuning

The thresholds that decide what's worth flagging live in ffmonitor/config.py: how
many points a bench player has to beat a starter by, what counts as a weak
starting spot, when a free agent is "hot," and how lopsided a game has to be to
matter. The machine-learning features are on by default in the workflow and
degrade gracefully: if the history data is ever unavailable, the core monitor
still runs.

## How it's built

The code is organized as a small Python package, ffmonitor, with clear pieces:
config and secrets, a normalized player format so every league looks the same to
the rest of the code, the ESPN client, snapshotting and day-over-day diffing, the
analysis and alerting, the machine-learning modules, the backtesting harness, and
the question-answering assistant. There's a test suite that runs automatically on
every push.

## A note on honesty

I was careful to keep the claims honest. The backtest only ever uses information
that was available at decision time. The results are described as backtested, not
as guarantees. And the live record grades itself with real outcomes, so over a
season it speaks for itself.
