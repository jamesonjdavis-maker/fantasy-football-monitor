/**
 * Cloudflare Worker: Discord `/ask` slash command for the Fantasy Football Monitor.
 *
 * Flow:
 *   1. Discord POSTs an "interaction" here when someone runs `/ask question:...`.
 *   2. We verify Discord's Ed25519 signature (required).
 *   3. Answer a PING with a PONG (Discord uses this to validate the endpoint).
 *   4. For the command: immediately return a *deferred* reply (so Discord sees a
 *      response within 3s), then in the background pull league context, call
 *      OpenRouter, and edit the deferred reply with the answer.
 *
 * Grounding: if GITHUB_TOKEN + GITHUB_REPO are set, we fetch the latest snapshot
 * (data/latest.json) and summarize it so answers are about YOUR teams. Without
 * them, it still answers, just without live roster context.
 *
 * No npm dependencies — Ed25519 verification uses the Workers WebCrypto API.
 *
 * Secrets/vars (set via `wrangler secret put` or the dashboard):
 *   DISCORD_PUBLIC_KEY   (var)    your Discord app's Public Key
 *   DISCORD_APP_ID       (var)    your Discord application (client) ID
 *   OPENROUTER_API_KEY   (secret) your OpenRouter key
 *   OPENROUTER_MODEL     (var)    optional, default openai/gpt-4o-mini
 *   GITHUB_TOKEN         (secret) optional, read-only token to fetch the snapshot
 *   GITHUB_REPO          (var)    optional, e.g. jamesonjdavis-maker/fantasy-football-monitor
 */

const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const DEFAULT_MODEL = "openai/gpt-4o-mini";

export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET") {
      return new Response("FF Ask bot is running.", { status: 200 });
    }
    const signature = request.headers.get("x-signature-ed25519");
    const timestamp = request.headers.get("x-signature-timestamp");
    const body = await request.text();

    if (!(await verify(env.DISCORD_PUBLIC_KEY, signature, timestamp, body))) {
      return new Response("invalid request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    // PING -> PONG (endpoint validation)
    if (interaction.type === 1) return json({ type: 1 });

    // APPLICATION_COMMAND
    if (interaction.type === 2) {
      const name = interaction.data.name;
      const opts = interaction.data.options || [];

      // /ranges [league] — deterministic floor/median/ceiling for a team.
      if (name === "ranges") {
        const league = (opts.find((o) => o.name === "league")?.value || "").toString();
        ctx.waitUntil(rangesLater(interaction, league, env));
        return json({ type: 5 });
      }

      // /trade give:... get:... — compare two players' value across the league.
      if (name === "trade") {
        const give = (opts.find((o) => o.name === "give")?.value || "").toString();
        const get = (opts.find((o) => o.name === "get")?.value || "").toString();
        ctx.waitUntil(tradeLater(interaction, give, get, env));
        return json({ type: 5 });
      }

      // /ask question:... — LLM answer grounded in your data.
      const question = (opts.find((o) => o.name === "question")?.value || "").toString().trim();
      if (!question) {
        return json({ type: 4, data: { content: "Ask me something with `/ask question:...`" } });
      }
      ctx.waitUntil(answerLater(interaction, question, env));
      return json({ type: 5 }); // DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    }

    return json({ type: 4, data: { content: "Unsupported interaction." } });
  },
};

async function answerLater(interaction, question, env) {
  let text;
  try {
    const context = await buildContext(env);
    text = await askOpenRouter(question, context, env);
  } catch (err) {
    text = `Sorry — I couldn't answer that (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

async function rangesLater(interaction, league, env) {
  let text;
  try {
    const snap = await fetchSnapshot(env);
    text = snap ? summarizeRanges(snap, league) : "No league data available.";
  } catch (err) {
    text = `Sorry — couldn't build ranges (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

async function tradeLater(interaction, give, get, env) {
  let text;
  try {
    const snap = await fetchSnapshot(env);
    if (!snap) {
      text = "No league data available yet.";
    } else {
      const g = findPlayer(snap, give);
      const r = findPlayer(snap, get);
      if (!g) text = `Couldn't find a player matching "${give}".`;
      else if (!r) text = `Couldn't find a player matching "${get}".`;
      else {
        text = tradeVerdict(g, r);
        const note = await tradeCommentary(g, r, env).catch(() => "");
        if (note) text += `\n\n💬 ${note}`;
      }
    }
  } catch (err) {
    text = `Sorry — couldn't evaluate the trade (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

// Normalize a name for forgiving matching: lowercase, strip accents and
// punctuation, drop Jr./Sr./II/III suffixes, collapse spaces.
function normName(s) {
  return (s || "")
    .toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/['’.]/g, "")            // drop apostrophes/periods: Ja'Marr -> jamarr, T.J. -> tj
    .replace(/[^a-z0-9 ]/g, " ")       // other punctuation (hyphens) -> space
    .replace(/\b(jr|sr|ii|iii|iv|v)\b/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Match if the query is contained in the name, or every query word appears in it
// (so "st brown", "amon brown", or "jefferson" all find the right player).
function nameMatches(name, query) {
  const n = normName(name), q = normName(query);
  if (!q) return false;
  if (n.includes(q)) return true;
  const words = new Set(n.split(" "));
  return q.split(" ").every((w) => words.has(w));
}

// Find a player by name anywhere in your leagues (your roster, other teams'
// rosters, or free agents), returning the player, league, owner, and that
// league's replacement levels (for value-over-replacement).
function findPlayer(snap, query) {
  if (!normName(query)) return null;
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    const repl = s.replacement_levels || {};
    for (const p of s.roster || [])
      if (nameMatches(p.name, query)) return { p, league, owner: s.team_name || "you", repl };
    for (const [team, players] of Object.entries(s.league_rosters || {}))
      for (const p of players || [])
        if (nameMatches(p.name, query)) return { p, league, owner: team, repl };
    for (const p of s.free_agents || [])
      if (nameMatches(p.name, query)) return { p, league, owner: "free agent", repl };
  }
  return null;
}

function valueOf(found) {
  const p = found.p;
  const med = p.proj_median != null ? p.proj_median : p.proj_points;
  if (med == null) return null;
  const rep = found.repl[p.position];
  return rep != null ? +(med - rep).toFixed(1) : +Number(med).toFixed(1);
}

function rangeStr(p) {
  if (p.proj_floor != null && p.proj_ceiling != null)
    return `range ${p.proj_floor}–${p.proj_ceiling} (mid ${p.proj_median ?? "?"})`;
  return p.proj_points != null ? `proj ~${p.proj_points}` : "no projection";
}

function valStr(v) {
  return v == null ? "" : `, value ${v > 0 ? "+" : ""}${v}`;
}

function tradeVerdict(g, r) {
  const gv = valueOf(g), rv = valueOf(r);
  const lines = ["🔁 **Trade check**"];
  const team = (p) => (p.pro_team ? ", " + p.pro_team : "");
  lines.push(`**Give:** ${g.p.name} (${g.p.position}${team(g.p)}) — ${rangeStr(g.p)}${valStr(gv)}`);
  lines.push(`**Get:** ${r.p.name} (${r.p.position}${team(r.p)}, ${r.owner}) — ${rangeStr(r.p)}${valStr(rv)}`);
  if (g.league !== r.league)
    lines.push(`⚠️ Different leagues (${g.league} vs ${r.league}) — a trade only works within one league.`);
  if (gv != null && rv != null) {
    const diff = +(rv - gv).toFixed(1);
    if (Math.abs(diff) < 1) lines.push("**Verdict:** roughly even on value.");
    else if (diff > 0) lines.push(`**Verdict:** favors you by about ${diff} points of value-over-replacement.`);
    else lines.push(`**Verdict:** you'd give up about ${Math.abs(diff)} points of value-over-replacement.`);
    lines.push("_Value = median projection minus a replacement-level player at that position. Positional need and the human side of the deal are still your call._");
  }
  return lines.join("\n");
}

async function tradeCommentary(g, r, env) {
  if (!env.OPENROUTER_API_KEY) return "";
  const facts =
    `Give ${g.p.name} (${g.p.position}, value ${valueOf(g)}, ${rangeStr(g.p)}); ` +
    `Get ${r.p.name} (${r.p.position}, value ${valueOf(r)}, ${rangeStr(r.p)}).`;
  const answer = await askOpenRouter(
    "In one or two sentences, give a plain take on this fantasy trade for me.",
    facts, env
  );
  return answer;
}

// Edit the deferred reply with the final content (Discord content cap ~2000).
async function editReply(interaction, env, text) {
  const url = `https://discord.com/api/v10/webhooks/${env.DISCORD_APP_ID}/${interaction.token}/messages/@original`;
  await fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: (text || "").slice(0, 1900) || "(nothing to show)" }),
  });
}

async function askOpenRouter(question, context, env) {
  const system =
    "You are a concise, sharp fantasy football assistant. Use ONLY the league " +
    "data provided below to give specific start/sit, waiver, and matchup advice " +
    "for the user's own teams. Refer to players and leagues by name. If the data " +
    "doesn't cover what's asked, say so briefly. Projections are weekly points; " +
    "ranges in [brackets] are Monte Carlo floor-ceiling. Keep answers under ~180 " +
    "words.\n\nLEAGUE DATA:\n" + (context || "No live league data available.");

  const resp = await fetch(OPENROUTER_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
      "Content-Type": "application/json",
      "X-Title": "Fantasy Football Monitor",
    },
    body: JSON.stringify({
      model: env.OPENROUTER_MODEL || DEFAULT_MODEL,
      messages: [
        { role: "system", content: system },
        { role: "user", content: question },
      ],
      max_tokens: 500,
      temperature: 0.4,
    }),
  });
  if (!resp.ok) throw new Error(`OpenRouter ${resp.status}`);
  const data = await resp.json();
  return (data.choices?.[0]?.message?.content || "").trim();
}

// --- League context (optional, needs GITHUB_TOKEN + GITHUB_REPO) --------------
async function fetchSnapshot(env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return null;
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/contents/data/latest.json`,
    {
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github.raw+json",
        "User-Agent": "ff-ask-bot",
      },
    }
  );
  if (!resp.ok) return null;
  return await resp.json();
}

async function buildContext(env) {
  const snap = await fetchSnapshot(env);
  return snap ? summarize(snap) : "";
}

// --- /ranges: floor / median / ceiling for each player, no LLM ----------------
function fmtRange(p) {
  const f = p.proj_floor, m = p.proj_median, c = p.proj_ceiling;
  if (f == null || c == null) return `• ${p.name} (${p.position}): no projection`;
  const mid = m != null ? `, mid ${m}` : "";
  return `• ${p.name} (${p.position}): floor ${f}${mid}, ceiling ${c}`;
}

function summarizeRanges(snap, leagueFilter) {
  const wanted = (leagueFilter || "").toLowerCase().trim();
  const blocks = [];
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const name = s.league_name || s.label || key;
    if (wanted && !name.toLowerCase().includes(wanted)) continue;
    const roster = s.roster || [];
    const starters = roster.filter((p) => p.slot === "starter");
    const bench = roster.filter((p) => p.slot === "bench");
    let block = `🎲 **${name}** — Week ${s.week ?? "?"} ranges (floor → ceiling)`;
    if (starters.length) block += "\n__Starters__\n" + starters.map(fmtRange).join("\n");
    if (bench.length) block += "\n__Bench__\n" + bench.map(fmtRange).join("\n");
    blocks.push(block);
  }
  if (!blocks.length) {
    return wanted
      ? `No league matching "${leagueFilter}". Try /ranges with no league to see them all.`
      : "No league data available yet.";
  }
  let out = blocks.join("\n\n");
  if (out.length > 1900) {
    out = out.slice(0, 1850) + "\n…(truncated — use `/ranges league:<name>` for one team)";
  }
  return out;
}

function fmtPlayer(p) {
  let s = `${p.name} (${p.position})`;
  if (p.proj_points != null) s += ` proj ${p.proj_points}`;
  if (p.proj_floor != null && p.proj_ceiling != null) s += ` [${p.proj_floor}-${p.proj_ceiling}]`;
  if (p.injury_status && p.injury_status !== "ACTIVE") s += ` ${p.injury_status}`;
  if (p.on_bye) s += " BYE";
  return s;
}

function summarize(snap) {
  const lines = [];
  const ss = snap.track_record?.start_sit;
  if (ss?.graded) {
    lines.push(`Start/sit record: ${ss.correct}-${ss.graded - ss.correct} (${ss.accuracy}%)`);
  }
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const name = s.league_name || s.label || key;
    let head = `\nLeague: ${name} (week ${s.week ?? "?"}`;
    if (s.opponent) head += `, vs ${s.opponent}`;
    head += `) - your team: ${s.team_name ?? "?"}`;
    lines.push(head);
    const roster = s.roster || [];
    const starters = roster.filter((p) => p.slot === "starter");
    const bench = roster.filter((p) => p.slot === "bench");
    if (starters.length) lines.push("  Starters: " + starters.map(fmtPlayer).join("; "));
    if (bench.length) lines.push("  Bench: " + bench.map(fmtPlayer).join("; "));
    const fas = (s.free_agents || [])
      .slice()
      .sort((a, b) => (b.proj_points || 0) - (a.proj_points || 0))
      .slice(0, 8);
    if (fas.length) lines.push("  Top free agents: " + fas.map(fmtPlayer).join("; "));
  }
  return lines.join("\n").trim();
}

// --- Ed25519 signature verification (WebCrypto, no dependencies) --------------
async function verify(publicKeyHex, signature, timestamp, body) {
  if (!publicKeyHex || !signature || !timestamp) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKeyHex),
      { name: "Ed25519" },
      false,
      ["verify"]
    );
    const message = new TextEncoder().encode(timestamp + body);
    return await crypto.subtle.verify({ name: "Ed25519" }, key, hexToBytes(signature), message);
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

function json(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { "Content-Type": "application/json" },
  });
}
