"""Ask a question about your leagues, answered by an LLM via OpenRouter.

The answer is grounded in your latest committed snapshot (rosters, projections,
floor/ceiling ranges, matchups, top free agents) plus your start/sit track
record — so it gives specific advice about *your* teams, not generic tips.

Triggered from the "Ask the assistant" GitHub Action (so you can ask from your
phone; the answer posts to Discord), or run locally:

    python -m ffmonitor.ask "Start Odunze or Fannin in the BBL this week?"

Environment:
  OPENROUTER_API_KEY   required — your OpenRouter key.
  OPENROUTER_MODEL     optional — default 'openai/gpt-4o-mini'. Any OpenRouter
                       model id works (e.g. 'anthropic/claude-3.5-haiku', or a
                       free model like 'meta-llama/llama-3.1-8b-instruct:free').
  DISCORD_WEBHOOK_URL  optional — post the answer to Discord; else it just prints.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "openai/gpt-4o-mini"
_TIMEOUT = 60
_MAX_FA = 8  # top free agents per league to include (keeps the prompt lean)

_SYSTEM = (
    "You are a concise, sharp fantasy football assistant. Use ONLY the league "
    "data provided below to give specific start/sit, waiver, and matchup advice "
    "for the user's own teams. Refer to players and leagues by name. If the data "
    "doesn't cover what's asked, say so briefly rather than guessing. Projections "
    "are weekly points; ranges in [brackets] are Monte Carlo floor–ceiling. Keep "
    "answers under ~200 words.\n\nLEAGUE DATA:\n{context}"
)


def _load_latest(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / "latest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _fmt_player(p: dict) -> str:
    s = f"{p.get('name')} ({p.get('position')})"
    if p.get("proj_points") is not None:
        s += f" proj {p['proj_points']}"
    if p.get("proj_floor") is not None and p.get("proj_ceiling") is not None:
        s += f" [{p['proj_floor']}-{p['proj_ceiling']}]"
    inj = p.get("injury_status")
    if inj and inj != "ACTIVE":
        s += f" {inj}"
    if p.get("on_bye"):
        s += " BYE"
    return s


def build_context(snapshot: dict, data_dir: Path) -> str:
    """A compact, LLM-friendly summary of the user's leagues."""
    lines: list[str] = []

    from .outcomes import summary, summary_line

    track = snapshot.get("track_record") or summary(data_dir)
    rec_line = summary_line(track)
    if rec_line:
        lines.append(rec_line)

    for key, snap in snapshot.get("platforms", {}).items():
        if not isinstance(snap, dict) or not snap.get("enabled"):
            continue
        name = snap.get("league_name") or snap.get("label") or key
        header = f"\nLeague: {name} (week {snap.get('week', '?')}"
        if snap.get("opponent"):
            header += f", vs {snap['opponent']}"
        header += f") — your team: {snap.get('team_name', '?')}"
        lines.append(header)

        roster = snap.get("roster", [])
        starters = [p for p in roster if p.get("slot") == "starter"]
        bench = [p for p in roster if p.get("slot") == "bench"]
        if starters:
            lines.append("  Starters: " + "; ".join(_fmt_player(p) for p in starters))
        if bench:
            lines.append("  Bench: " + "; ".join(_fmt_player(p) for p in bench))
        fas = sorted(snap.get("free_agents", []),
                     key=lambda p: p.get("proj_points") or 0, reverse=True)[:_MAX_FA]
        if fas:
            lines.append("  Top free agents: " + "; ".join(_fmt_player(p) for p in fas))

    return "\n".join(lines).strip() or "No league data available."


def ask_openrouter(question: str, context: str, model: str, api_key: str) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM.format(context=context)},
            {"role": "user", "content": question},
        ],
        "max_tokens": 700,
        "temperature": 0.4,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Optional attribution headers OpenRouter recommends.
        "X-Title": "Fantasy Football Monitor",
    }
    resp = requests.post(_OPENROUTER_URL, json=body, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected OpenRouter response: {data}")


def post_to_discord(webhook_url: str, question: str, answer: str) -> None:
    from .alerts import send_discord

    # Discord embed description caps at 4096 chars; split a long answer.
    chunks = [answer[i:i + 4000] for i in range(0, len(answer), 4000)] or ["(no answer)"]
    embeds = []
    for i, chunk in enumerate(chunks):
        embeds.append({
            "title": f"🤖 {question[:240]}" if i == 0 else "🤖 (cont.)",
            "description": chunk,
            "color": 0x5865F2,
        })
    send_discord(webhook_url, {"username": "FF Assistant", "embeds": embeds})


def run(question: str) -> int:
    from .config import Config

    config = Config.from_env()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("[ask] OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 2

    model = os.getenv("OPENROUTER_MODEL") or _DEFAULT_MODEL
    snapshot = _load_latest(config.data_dir)
    context = build_context(snapshot, config.data_dir)

    try:
        answer = ask_openrouter(question, context, model, api_key)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"[ask] OpenRouter request failed: {exc}", file=sys.stderr)
        return 1

    print(f"Q: {question}\n\nA: {answer}")

    if config.discord_webhook_url:
        try:
            post_to_discord(config.discord_webhook_url, question, answer)
            print("[ask] Answer posted to Discord.")
        except requests.RequestException as exc:
            print(f"[ask] Discord post failed: {exc}", file=sys.stderr)
    return 0


def main() -> None:
    # Question comes from CLI args, or the QUESTION env var (used by the workflow
    # so the value never touches the shell command line).
    question = " ".join(sys.argv[1:]).strip() or os.getenv("QUESTION", "").strip()
    if not question:
        print('Usage: python -m ffmonitor.ask "your question"', file=sys.stderr)
        sys.exit(2)
    sys.exit(run(question))


if __name__ == "__main__":
    main()
