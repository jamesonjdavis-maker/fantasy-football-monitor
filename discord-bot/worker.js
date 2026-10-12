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

    // APPLICATION_COMMAND_AUTOCOMPLETE — as-you-type suggestions in the client.
    // Must answer synchronously (no defer), so we respond from a short-lived
    // cache of the snapshot to stay well under Discord's 3s window.
    if (interaction.type === 4) return await autocomplete(interaction, env, ctx);

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

      // /trade give:... get:... [league:...] — compare value across the league.
      if (name === "trade") {
        const give = (opts.find((o) => o.name === "give")?.value || "").toString();
        const get = (opts.find((o) => o.name === "get")?.value || "").toString();
        const league = (opts.find((o) => o.name === "league")?.value || "").toString();
        ctx.waitUntil(tradeLater(interaction, give, get, league, env));
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

// --- Autocomplete: suggest players (and leagues) as the user types ------------
// Discord fires this on each keystroke, so we read the snapshot from a 60s cache
// and always reply with a type-8 (choices) response, even if empty.
async function autocomplete(interaction, env, ctx) {
  try {
    const opts = interaction.data.options || [];
    const focused = opts.find((o) => o.focused);
    if (!focused) return json({ type: 8, data: { choices: [] } });
    const snap = await fetchSnapshotCached(env, ctx);
    if (!snap) return json({ type: 8, data: { choices: [] } });

    // The `league` field: suggest the league names themselves.
    if (focused.name === "league") {
      const q = normName(focused.value || "");
      const choices = leagueNames(snap)
        .filter((n) => !q || normName(n).includes(q))
        .slice(0, 25)
        .map((n) => ({ name: n.slice(0, 100), value: n.slice(0, 100) }));
      return json({ type: 8, data: { choices } });
    }

    // give / get: complete the LAST comma-separated name, keeping earlier ones.
    const leagueOpt = (opts.find((o) => o.name === "league")?.value || "").toString();
    const raw = String(focused.value || "");
    const lastComma = raw.lastIndexOf(",");
    const prefix = lastComma >= 0 ? raw.slice(0, lastComma + 1) + " " : "";
    const partial = (lastComma >= 0 ? raw.slice(lastComma + 1) : raw).trim();
    return json({ type: 8, data: { choices: playerChoices(snap, partial, leagueOpt, prefix) } });
  } catch {
    return json({ type: 8, data: { choices: [] } });
  }
}

function leagueNames(snap) {
  const names = [];
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    names.push(s.league_name || s.label || key);
  }
  return [...new Set(names)];
}

// Every distinct player across the (optionally league-scoped) leagues, deduped
// by name so one person doesn't appear once per league they're rostered in.
function collectPlayers(snap, leagueFilter) {
  const lf = normName(leagueFilter || "");
  const out = new Map();
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;
    const add = (p) => {
      if (!p || !p.name) return;
      const k = normName(p.name);
      if (!out.has(k)) out.set(k, { name: p.name, position: p.position, pro_team: p.pro_team });
    };
    (s.roster || []).forEach(add);
    for (const players of Object.values(s.league_rosters || {})) (players || []).forEach(add);
    (s.free_agents || []).forEach(add);
  }
  return [...out.values()];
}

// Up to 25 choices. `value` is the full field text with the last name completed,
// so earlier comma-separated picks are preserved when Discord replaces the input.
function playerChoices(snap, partial, leagueFilter, prefix) {
  let players = collectPlayers(snap, leagueFilter);
  if (partial) players = players.filter((p) => nameMatches(p.name, partial));
  players.sort((a, b) => a.name.localeCompare(b.name));
  const choices = [], seen = new Set();
  for (const p of players) {
    const value = (prefix + p.name).slice(0, 100);
    if (seen.has(value)) continue;
    seen.add(value);
    const team = p.pro_team ? ", " + p.pro_team : "";
    choices.push({ name: `${p.name} (${p.position}${team})`.slice(0, 100), value });
    if (choices.length >= 25) break;
  }
  return choices;
}

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

async function tradeLater(interaction, give, get, league, env) {
  let text;
  try {
    const snap = await fetchSnapshot(env);
    if (!snap) {
      text = "No league data available yet.";
    } else {
      const g = findPlayers(snap, give, league);
      const r = findPlayers(snap, get, league);
      if (!g.found.length && !r.found.length) {
        text = league
          ? `Couldn't find those players in a league matching "${league}".`
          : `Couldn't find any players matching "${give}" or "${get}".`;
      } else {
        text = tradeVerdict(g, r);
        const context = rosterContext(snap, g, r);
        const note = await tradeCommentary(g, r, context, env).catch(() => "");
        if (note) text += `\n\n💬 ${note}`;
      }
    }
  } catch (err) {
    text = `Sorry — couldn't evaluate the trade (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

// A compact view of both teams' depth by position, so the AI can weigh need.
function teamByPosition(players) {
  const by = {};
  for (const p of players || []) by[p.position] = (by[p.position] || 0) + 1;
  return Object.entries(by).sort().map(([pos, n]) => `${pos} ${n}`).join(", ") || "n/a";
}

function rosterContext(snap, g, r) {
  const leagues = [...new Set([...g.found, ...r.found].map((f) => f.league))];
  if (leagues.length !== 1) return "";
  const leagueName = leagues[0];
  const plat = Object.values(snap.platforms || {}).find(
    (s) => s && s.enabled && (s.league_name || s.label) === leagueName
  );
  if (!plat) return "";
  const partners = [...new Set(r.found.map((f) => f.owner).filter((o) => o && o !== "free agent" && o !== plat.team_name))];
  const partnerPlayers = partners.flatMap((t) => (plat.league_rosters || {})[t] || []);
  let ctx = `League: ${leagueName}. Your team (${plat.team_name}) roster by position: ${teamByPosition(plat.roster)}.`;
  if (partnerPlayers.length)
    ctx += ` Trade partner (${partners.join(", ")}) roster by position: ${teamByPosition(partnerPlayers)}.`;
  return ctx;
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
function findPlayer(snap, query, leagueFilter) {
  if (!normName(query)) return null;
  const lf = normName(leagueFilter || "");
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;  // scope to a league if asked
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

// Parse a comma-separated list of names and look each one up, so a side of a
// trade can be one player or several.
function findPlayers(snap, query, leagueFilter) {
  const found = [], missing = [];
  for (const name of String(query || "").split(",").map((s) => s.trim()).filter(Boolean)) {
    const hit = findPlayer(snap, name, leagueFilter);
    if (hit) found.push(hit);
    else missing.push(name);
  }
  return { found, missing };
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

// Sum value and median projection for one side of a trade.
function sideTotals(found) {
  let value = 0, med = 0, hasValue = false;
  for (const f of found) {
    const v = valueOf(f);
    if (v != null) { value += v; hasValue = true; }
    const m = f.p.proj_median != null ? f.p.proj_median : f.p.proj_points;
    if (m != null) med += m;
  }
  return { value: +value.toFixed(1), med: +med.toFixed(1), hasValue };
}

function sideLines(label, side) {
  const lines = [`**${label}:**`];
  for (const f of side.found) {
    const team = f.p.pro_team ? ", " + f.p.pro_team : "";
    lines.push(`• ${f.p.name} (${f.p.position}${team}) — ${rangeStr(f.p)}${valStr(valueOf(f))}`);
  }
  for (const name of side.missing) lines.push(`• ⚠️ couldn't find "${name}"`);
  return lines;
}

function tradeVerdict(g, r) {
  const lines = ["🔁 **Trade check**"];
  const leagues = new Set([...g.found, ...r.found].map((f) => f.league));
  if (leagues.size === 1) lines[0] += ` (${[...leagues][0]})`;

  const gt = sideTotals(g.found), rt = sideTotals(r.found);
  lines.push(...sideLines("Give", g));
  if (g.found.length > 1) lines.push(`→ total value ${gt.value > 0 ? "+" : ""}${gt.value} (mid ~${gt.med})`);
  lines.push(...sideLines("Get", r));
  if (r.found.length > 1) lines.push(`→ total value ${rt.value > 0 ? "+" : ""}${rt.value} (mid ~${rt.med})`);

  if (leagues.size > 1)
    lines.push(`⚠️ Players span different leagues (${[...leagues].join(", ")}) — a trade only works within one league.`);
  if (g.missing.length || r.missing.length)
    lines.push("⚠️ Some players weren't found, so the verdict is incomplete.");

  if (gt.hasValue && rt.hasValue) {
    const diff = +(rt.value - gt.value).toFixed(1);
    if (Math.abs(diff) < 1) lines.push("**Verdict:** roughly even on value.");
    else if (diff > 0) lines.push(`**Verdict:** favors you by about ${diff} points of value-over-replacement.`);
    else lines.push(`**Verdict:** you'd give up about ${Math.abs(diff)} points of value-over-replacement.`);
    lines.push("_Value = median projection minus a replacement-level player at that position. Depth, positional need, and the human side of the deal are still your call._");
  }
  return lines.join("\n");
}

async function tradeCommentary(g, r, context, env) {
  if (!env.OPENROUTER_API_KEY || (!g.found.length && !r.found.length)) return "";
  const desc = (f) => `${f.p.name} (${f.p.position}, value ${valueOf(f)}, ${rangeStr(f.p)})`;
  const facts =
    `Give: ${g.found.map(desc).join("; ") || "nobody"}. ` +
    `Get: ${r.found.map(desc).join("; ") || "nobody"}. ` +
    (context || "");
  return await askOpenRouter(
    "In two or three sentences, give a plain, optimized take on this multi-player fantasy " +
    "trade: weigh total value, roster depth, and positional need (does it fill a hole or " +
    "create one?). Use the roster-by-position info if given.",
    facts, env
  );
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

// Cache the snapshot ~60s so rapid autocomplete keystrokes don't hammer GitHub
// (and each one stays fast enough for Discord's 3s autocomplete window).
async function fetchSnapshotCached(env, ctx) {
  const cacheKey = new Request("https://ff-bot-cache.local/latest.json");
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return await hit.json();
  const snap = await fetchSnapshot(env);
  if (snap) {
    const toCache = new Response(JSON.stringify(snap), {
      headers: { "Content-Type": "application/json", "Cache-Control": "max-age=60" },
    });
    if (ctx) ctx.waitUntil(cache.put(cacheKey, toCache));
  }
  return snap;
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
