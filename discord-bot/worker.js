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

// ============================================================================
// TRADE TUNING  —  the judgment-call weights behind every trade decision.
// These are the only "opinion" knobs. Everything else (per-game projections,
// defense-vs-position, Vegas, weather, injuries) is a measured input, not a
// weight. Adjust these to change how the tool thinks about trades; see README
// section 11 for what each one does. The baseline projection blend (ESPN vs
// realized form) lives in ffmonitor/platforms/espn.py, which has its own block.
// ============================================================================
const TRADE_TUNING = {
  // --- Player value: risk blend of the Monte Carlo range ---
  // The per-game value starts from the mean projection (which itself already blends
  // ESPN's projection, realized form, defense, Vegas, weather and injuries) and is
  // then nudged by the shape of the player's floor/median/ceiling range: upside above
  // the median adds value, downside below the median subtracts it, both relative to
  // the median so it is scale-independent. A steady (tight-range) player stays at the
  // mean; a boom player gains; a bust-risk player loses. The whole nudge is capped so
  // it stays a tie-breaker, not the main driver. Set both weights to 0 to use the
  // pure mean, or FLOOR_WEIGHT to 0 to ignore downside.
  VALUE_BLEND: {
    CEILING_WEIGHT: 0.15,  // reward upside (ceiling above the median)
    FLOOR_WEIGHT: 0.15,    // penalize downside (floor below the median)
    CAP: 0.15,             // keep the total adjustment within +/- this fraction
  },

  // Rest-of-season confidence multiplier for a player carrying an injury
  // designation. The weekly projection already zeroes the weeks he is known to
  // miss and docks the near term, but averaged over a whole season that barely
  // moves his value, so a player who might not play is still valued almost like a
  // lock. This is the extra, uniform haircut for that uncertainty (a longer
  // absence, a limited return) that a trade cannot see. 1.0 = no extra discount;
  // lower = fade injured players harder. Applied to value AND the lineup grade.
  INJURY_CONFIDENCE: {
    QUESTIONABLE: 0.90,
    DOUBTFUL: 0.80,
    OUT: 0.85,
    IR: 0.75,
    PUP: 0.75,
    SUSPENDED: 1.0,   // a suspension has a known length; the zeroed weeks handle it
  },

  // --- Grade: starting-lineup impact (dominant) + a little bench depth ---
  DEPTH_WEIGHT: 0.1,   // how much the change in bench value counts toward the grade
  DEPTH_CAP: 1.0,      // but never more than this many pts/wk either way
  // Letter-grade cutoffs in starting pts/wk: the first row whose threshold the
  // score meets (score >= threshold) wins. Keep them in descending order.
  GRADE_CUTOFFS: [
    [4,         "A",  "big upgrade"],
    [2,         "B",  "clear upgrade"],
    [0.7,       "B-", "slight upgrade"],
    [-0.7,      "C",  "about even"],
    [-2,        "C-", "slight downgrade"],
    [-4,        "D",  "downgrade"],
    [-Infinity, "F",  "big downgrade"],
  ],

  // --- Trade finder: proposing deals (need-first, unlike the analyzer) ---
  STARTERS: { QB: 1, RB: 2, WR: 2, TE: 1 }, // typical startable depth per position
  // A position is a real need when your weakest starter there projects below this
  // fraction of the LEAGUE median starter at that position (or the slot is empty).
  // The bar is the competitive field, not the waiver wire: waiver level is so low
  // that even a bad team clears it, which hid real holes; and it is position-fair,
  // so a deep position like QB (whose starters cluster tightly and high) is only a
  // need when your QB truly lags the field, not merely because waiver QBs are
  // plentiful. Lower this to flag fewer needs, raise it toward 1 to flag more.
  NEED_FRACTION: 0.85,
  SET_POS_DISCOUNT: 0.2,  // upgrades at a position you're already set at count this fraction
  MIN_GAIN: 0.5,          // a proposed deal must improve each team's lineup by at least this
  FAIR_BAND: 0.25,        // allowed value gap = max(FAIR_FLOOR, this * the bigger side)
  FAIR_FLOOR: 4,          // ...but always allow at least this absolute value gap
  GAP_PENALTY: 0.35,      // ranking: how much an unfair value gap hurts a candidate
  SIZE_PENALTY: 0.4,      // ranking: penalty per player involved (favors tidy deals)
};

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

      // /trade give1/get1 [give2/get2/give3/get3] [league] — up to 3 per side.
      if (name === "trade") {
        const join = (ns) => ns.map((n) => opts.find((o) => o.name === n)?.value).filter(Boolean).join(", ");
        const give = join(["give1", "give2", "give3"]);
        const get = join(["get1", "get2", "get3"]);
        const league = (opts.find((o) => o.name === "league")?.value || "").toString();
        const teamGive = (opts.find((o) => o.name === "team_give")?.value || "").toString();
        const teamGet = (opts.find((o) => o.name === "team_get")?.value || "").toString();
        ctx.waitUntil(tradeLater(interaction, give, get, league, teamGive, teamGet, env));
        return json({ type: 5 });
      }

      // /tradefinder team:... [league] [my_team] — suggest mutually useful deals.
      if (name === "tradefinder") {
        const team = (opts.find((o) => o.name === "team")?.value || "").toString();
        const league = (opts.find((o) => o.name === "league")?.value || "").toString();
        const myTeam = (opts.find((o) => o.name === "my_team")?.value || "").toString();
        ctx.waitUntil(tradeFinderLater(interaction, team, league, myTeam, env));
        return json({ type: 5 });
      }

      // /projections player:... [league:...] — week-by-week ROS projections.
      if (name === "projections") {
        const player = (opts.find((o) => o.name === "player")?.value || "").toString();
        const league = (opts.find((o) => o.name === "league")?.value || "").toString();
        ctx.waitUntil(projectionsLater(interaction, player, league, env));
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

    const leagueOpt = (opts.find((o) => o.name === "league")?.value || "").toString();

    // The `league` field: suggest the league names themselves.
    if (focused.name === "league") {
      const q = normName(focused.value || "");
      const choices = leagueNames(snap)
        .filter((n) => !q || normName(n).includes(q))
        .slice(0, 25)
        .map((n) => ({ name: n.slice(0, 100), value: n.slice(0, 100) }));
      return json({ type: 8, data: { choices } });
    }

    // The team fields: suggest the league's team names.
    if (["team_give", "team_get", "team", "my_team"].includes(focused.name)) {
      const q = normName(focused.value || "");
      const choices = teamNames(snap, leagueOpt)
        .filter((n) => !q || normName(n).includes(q))
        .slice(0, 25)
        .map((n) => ({ name: n.slice(0, 100), value: n.slice(0, 100) }));
      return json({ type: 8, data: { choices } });
    }

    // A player field (give1/get2/.../player): a single pick. Search by what's
    // typed so far; scope to the league, and — for a trade side — to that side's
    // team if named; and hide players already chosen in another trade slot.
    const side = focused.name.startsWith("give")
      ? "team_give"
      : focused.name.startsWith("get")
        ? "team_get"
        : null;
    const teamOpt = side ? (opts.find((o) => o.name === side)?.value || "").toString() : "";
    const partial = String(focused.value || "").trim();
    const playerSlots = new Set(["give1", "give2", "give3", "get1", "get2", "get3"]);
    const exclude = new Set(
      opts
        .filter((o) => o.name !== focused.name && playerSlots.has(o.name))
        .map((o) => normName(o.value || ""))
        .filter(Boolean)
    );
    const players = collectPlayers(snap, leagueOpt, teamOpt);
    return json({ type: 8, data: { choices: playerChoices(players, partial, exclude) } });
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

// League team names (the user's own plus every other manager), for the team
// dropdowns.
function teamNames(snap, leagueFilter) {
  const lf = normName(leagueFilter || "");
  const names = new Set();
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;
    if (s.team_name) names.add(s.team_name);
    for (const team of Object.keys(s.league_rosters || {})) names.add(team);
  }
  return [...names];
}

// Every distinct player across the (optionally league-scoped) leagues, deduped
// by name. When teamFilter is given, only that team's rostered players are
// returned (free agents are left out).
function collectPlayers(snap, leagueFilter, teamFilter) {
  const lf = normName(leagueFilter || "");
  const tf = normName(teamFilter || "");
  const out = new Map();
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;
    const add = (p, owner) => {
      if (!p || !p.name) return;
      if (tf && !normName(owner).includes(tf)) return;
      const k = normName(p.name);
      if (!out.has(k)) out.set(k, { name: p.name, position: p.position, pro_team: p.pro_team });
    };
    (s.roster || []).forEach((p) => add(p, s.team_name || "you"));
    for (const [team, players] of Object.entries(s.league_rosters || {}))
      (players || []).forEach((p) => add(p, team));
    if (!tf) (s.free_agents || []).forEach((p) => add(p, "free agent"));
  }
  return [...out.values()];
}

// Up to 25 single-player choices matching what's typed, skipping players already
// chosen in another slot (exclude).
function playerChoices(players, partial, exclude) {
  let list = players.slice();
  if (partial) list = list.filter((p) => nameMatches(p.name, partial));
  if (exclude && exclude.size) list = list.filter((p) => !exclude.has(normName(p.name)));
  list.sort((a, b) => a.name.localeCompare(b.name));
  const choices = [], seen = new Set();
  for (const p of list) {
    const k = normName(p.name);
    if (seen.has(k)) continue;
    seen.add(k);
    const team = p.pro_team ? ", " + p.pro_team : "";
    choices.push({ name: `${p.name} (${p.position}${team})`.slice(0, 100), value: p.name.slice(0, 100) });
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

async function tradeLater(interaction, give, get, league, teamGive, teamGet, env) {
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
        // Send the card first, then the AI take as a separate follow-up message,
        // so the two never share Discord's 2000-char limit and get truncated.
        const impact = tradeTeamImpact(snap, g, r, teamGive, teamGet);
        const card = tradeVerdict(g, r, teamGive, teamGet, impact);
        await editReply(interaction, env, card);
        // The follow-up leads with a code-written summary (always correct), then
        // the AI take as color below it. Even if the AI slips, the facts above it
        // are the source of truth.
        const summary = deterministicSummary(g, r, teamGive, teamGet, impact);
        const context = rosterContext(snap, g, r, teamGive, teamGet);
        const note = await tradeCommentary(g, r, context, env, teamGive, teamGet, impact).catch(() => "");
        const parts = [];
        if (summary) parts.push(`📋 **The deal, by the numbers:**\n${summary}`);
        if (note) parts.push(`💬 ${note}`);
        if (parts.length) await sendFollowup(interaction, env, parts.join("\n\n"));
        return;
      }
    }
  } catch (err) {
    text = `Sorry — couldn't evaluate the trade (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

// --- /tradefinder: suggest mutually useful deals with a specific team ---------

function playerValue(p, repl) {
  const basis = tradeBasis(p);
  if (basis == null) return null;
  const rep = repl[p.position];
  return rep != null ? +(basis - rep).toFixed(1) : +Number(basis).toFixed(1);
}

// Group a roster's startable players by position, sorted by value (best first).
// Each entry carries both value-over-replacement `v` (for gain math) and the raw
// per-game basis `b` (for the competitive need test).
function byPositionValued(players, repl) {
  const by = {};
  for (const p of players || []) {
    if (!["QB", "RB", "WR", "TE"].includes(p.position)) continue;
    const v = playerValue(p, repl);
    if (v == null) continue;
    (by[p.position] = by[p.position] || []).push({ p, v, b: tradeBasis(p) });
  }
  for (const pos of Object.keys(by)) by[pos].sort((a, b) => b.v - a.v);
  return by;
}

// The competitive bar per position: the median per-game value of the players
// teams actually START there (each team's top STARTERS[pos]), across the whole
// league. A need is judged against this, not the waiver replacement level, so a
// genuinely weak starter is caught even when it still clears the waiver wire, and
// a deep position like QB (starters bunched near the top) is only flagged when a
// team truly lags the field.
function leagueStarterBar(rosters) {
  const byPos = { QB: [], RB: [], WR: [], TE: [] };
  for (const players of rosters) {
    const t = {};
    for (const p of players || []) {
      if (!TRADE_TUNING.STARTERS[p.position]) continue;
      const b = tradeBasis(p);
      if (b != null) (t[p.position] = t[p.position] || []).push(b);
    }
    for (const pos in t) {
      t[pos].sort((a, b) => b - a);
      t[pos].slice(0, TRADE_TUNING.STARTERS[pos]).forEach((v) => byPos[pos].push(v));
    }
  }
  const bar = {};
  for (const pos of ["QB", "RB", "WR", "TE"]) {
    const a = byPos[pos].sort((x, y) => x - y);
    bar[pos] = a.length ? a[Math.floor((a.length - 1) / 2)] : 0;  // median starter
  }
  return bar;
}

// Each team by position: the value of its weakest starter (the bar a new player
// must clear to be an upgrade), its tradeable bench depth beyond the starters, and
// whether the position is a genuine need (weakest starter below the league bar, or
// the slot is unfilled).
function teamShape(by, bar) {
  const weakestStarter = {}, bench = {}, need = {};
  for (const pos of ["QB", "RB", "WR", "TE"]) {
    const list = by[pos] || [];
    const req = TRADE_TUNING.STARTERS[pos];
    const starters = list.slice(0, req);
    weakestStarter[pos] = starters.length ? starters[starters.length - 1].v : -5;  // empty slot = easy upgrade
    bench[pos] = list.slice(req);  // depth they could deal
    const weakestBasis = starters.length ? starters[starters.length - 1].b : null;
    const competitive = (bar[pos] || 0) * TRADE_TUNING.NEED_FRACTION;
    need[pos] = starters.length < req || (weakestBasis != null && weakestBasis < competitive);
  }
  return { weakestStarter, bench, need };
}

// Positions a team genuinely needs to upgrade (see teamShape / NEED_FRACTION).
// Positions that are not needs are "set", and the finder won't chase marginal
// upgrades there (see lineupGain).
function weakSpots(shape) {
  return ["QB", "RB", "WR", "TE"].filter((pos) => shape.need[pos]);
}

function findLeagueForTeam(snap, leagueFilter, partnerQuery) {
  const lf = normName(leagueFilter || ""), pq = normName(partnerQuery || "");
  for (const [k, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || k;
    if (lf && !normName(league).includes(lf)) continue;
    const partner = Object.keys(s.league_rosters || {}).find((t) => normName(t).includes(pq));
    if (partner) return { plat: s, league, partner };
  }
  return null;
}

// All non-empty subsets of arr up to maxSize.
function subsets(arr, maxSize) {
  const out = [[]];
  for (const item of arr) {
    const n = out.length;
    for (let i = 0; i < n; i++) {
      if (out[i].length < maxSize) out.push(out[i].concat([item]));
    }
  }
  return out.filter((s) => s.length >= 1);
}

const _POS = ["QB", "RB", "WR", "TE"];
const sumV = (bundle) => +bundle.reduce((a, b) => a + b.v, 0).toFixed(1);
const posSet = (bundle) => new Set(bundle.map((x) => x.pos));

// How much a bundle would improve the receiving team's lineup. This is where the
// finder differs from the analyzer: the analyzer grades a trade you already have
// in mind, so it scores any upgrade; the finder PROPOSES trades, so it should
// chase real needs, not push a marginal upgrade at a position you are already set
// at (the "why are you offering me a QB when my QB is fine?" problem). So for each
// position a bundle brings, we take how far its best piece clears the receiving
// team's weakest starter there, but a position that is NOT a need (the team is
// already competitive there) counts at a steep discount (see TRADE_TUNING).
function lineupGain(bundle, receivingShape) {
  const bestByPos = {};
  for (const x of bundle) bestByPos[x.pos] = Math.max(bestByPos[x.pos] ?? -Infinity, x.v);
  let g = 0;
  for (const pos of Object.keys(bestByPos)) {
    const over = bestByPos[pos] - receivingShape.weakestStarter[pos];
    if (over <= 0) continue;
    const isNeed = receivingShape.need[pos];
    g += isNeed ? over : over * TRADE_TUNING.SET_POS_DISCOUNT;
  }
  return +g.toFixed(1);
}

// Build mutually useful packages: give 1-3 of my bench depth for 1-2 of theirs,
// value-fair, where BOTH lineups get a real upgrade. Covers 1-for-1 up to 3-for-2.
function buildTradeIdeas(mineShape, theirsShape) {
  const pool = (shape) => {
    const out = [];
    for (const pos of _POS) for (const x of shape.bench[pos] || []) out.push({ p: x.p, v: x.v, pos });
    return out.sort((a, b) => b.v - a.v).slice(0, 6);  // top depth pieces, bounded
  };
  const mine = pool(mineShape), theirs = pool(theirsShape);

  const giveBundles = subsets(mine, 3);  // send up to 3
  const getBundles = subsets(theirs, 2); // receive up to 2
  const t = TRADE_TUNING;
  const ideas = [];
  for (const get of getBundles) {
    const myGain = lineupGain(get, mineShape);
    if (myGain < t.MIN_GAIN) continue;          // the deal must upgrade MY lineup
    const gt = sumV(get);
    for (const give of giveBundles) {
      const theirGain = lineupGain(give, theirsShape);
      if (theirGain < t.MIN_GAIN) continue;     // and THEIRS
      const gv = sumV(give);
      const gap = Math.abs(gt - gv);
      const band = Math.max(t.FAIR_FLOOR, t.FAIR_BAND * Math.max(Math.abs(gt), Math.abs(gv), 1));
      if (gap > band) continue;
      const size = give.length + get.length;
      const score = myGain + theirGain - gap * t.GAP_PENALTY - size * t.SIZE_PENALTY;
      ideas.push({ give, get, gv, rv: gt, score });
    }
  }
  ideas.sort((a, b) => b.score - a.score);
  const seen = new Set(), top = [];
  for (const i of ideas) {
    const key = i.give.map((x) => x.p.name).sort().join(",") + ">" + i.get.map((x) => x.p.name).sort().join(",");
    if (seen.has(key)) continue;
    seen.add(key);
    top.push(i);
    if (top.length >= 5) break;
  }
  return top;
}

function bundleStr(bundle) {
  return bundle.map((x) => `${x.p.name} (${x.p.position}, ${x.v > 0 ? "+" : ""}${x.v})`).join(" + ");
}

async function tradeFinderLater(interaction, partnerQuery, league, myTeamQuery, env) {
  let text;
  try {
    const snap = await fetchSnapshot(env);
    if (!snap) { await editReply(interaction, env, "No league data available yet."); return; }
    const found = findLeagueForTeam(snap, league, partnerQuery);
    if (!found) {
      await editReply(interaction, env, league
        ? `Couldn't find a team matching "${partnerQuery}" in a league matching "${league}".`
        : `Couldn't find a team matching "${partnerQuery}". Try adding league:.`);
      return;
    }
    const { plat, league: leagueName, partner } = found;
    const repl = seasonReplacement(plat);

    // My roster: the named team if given, else my own team in this league.
    let myName = plat.team_name, myPlayers = plat.roster;
    if (myTeamQuery) {
      const match = Object.keys(plat.league_rosters || {}).find((t) => normName(t).includes(normName(myTeamQuery)));
      if (match) { myName = match; myPlayers = (plat.league_rosters || {})[match]; }
    }
    if (normName(myName) === normName(partner)) {
      await editReply(interaction, env, "That's the same team on both sides — pick a different partner.");
      return;
    }
    const theirPlayers = (plat.league_rosters || {})[partner] || [];

    // Competitive need bar from every team in the league (my roster plus all the
    // league rosters, which already include the partner's).
    const bar = leagueStarterBar([plat.roster, ...Object.values(plat.league_rosters || {})]);
    const mineShape = teamShape(byPositionValued(myPlayers, repl), bar);
    const theirsShape = teamShape(byPositionValued(theirPlayers, repl), bar);
    const ideas = buildTradeIdeas(mineShape, theirsShape);

    const lines = [`🔎 **Trade ideas: ${myName} ↔ ${partner}** (${leagueName})`];
    const needStr = (shape) => weakSpots(shape).join(", ") || "none pressing";
    lines.push(`Your weak spots: ${needStr(mineShape)} · ${partner}'s weak spots: ${needStr(theirsShape)}`);
    if (!ideas.length) {
      lines.push("\nNo clean value-fair swaps where you each fill a need — your rosters may not line up, or needs overlap. Try a different partner, or use `/trade` to test a specific idea.");
      text = lines.join("\n");
    } else {
      lines.push("");
      ideas.forEach((i, n) => {
        const totals = (i.give.length > 1 || i.get.length > 1) ? `  _(${i.gv} ↔ ${i.rv})_` : "";
        lines.push(`**${n + 1}.** Give **${bundleStr(i.give)}** ↔ Get **${bundleStr(i.get)}**${totals}`);
      });
      lines.push("_Value = rest-of-season points/game over replacement. Ideas pair your surplus with their need and vice versa; the human side is still your call._");
      await editReply(interaction, env, lines.join("\n"));
      const note = await tradeFinderCommentary(myName, partner, ideas, mineShape, theirsShape, env).catch(() => "");
      if (note) await sendFollowup(interaction, env, `💬 ${note}`);
      return;
    }
  } catch (err) {
    text = `Sorry — couldn't build trade ideas (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

async function tradeFinderCommentary(myName, partner, ideas, mineShape, theirsShape, env) {
  if (!env.OPENROUTER_API_KEY || !ideas.length) return "";
  const list = ideas
    .map((i, n) => `${n + 1}) give ${bundleStr(i.give)} for ${bundleStr(i.get)}`)
    .join("; ");
  const needs = (s) => weakSpots(s).join("/") || "none";
  const facts =
    `My team: ${myName} (weak at ${needs(mineShape)}). Partner: ${partner} (weak at ${needs(theirsShape)}). ` +
    `Candidate swaps by rest-of-season value over replacement: ${list}.`;
  return await askOpenRouter(
    "You are a fantasy trade negotiator. From these candidate swaps, recommend the 2 best " +
    "deals to actually pitch and say in one line each why it works for BOTH teams (who fills " +
    "what need). Values are rest-of-season points per game over replacement. Be concise; " +
    "don't invent players not listed.",
    facts, env
  );
}

async function projectionsLater(interaction, player, league, env) {
  let text;
  try {
    const snap = await fetchSnapshot(env);
    if (!snap) {
      text = "No league data available yet.";
    } else {
      const hit = findWithProjections(snap, player, league);
      if (!hit) {
        text = league
          ? `Couldn't find "${player}" in a league matching "${league}".`
          : `Couldn't find a player matching "${player}".`;
      } else {
        text = projectionLines(hit);
      }
    }
  } catch (err) {
    text = `Sorry — couldn't pull projections (${String(err).slice(0, 200)}).`;
  }
  await editReply(interaction, env, text);
}

// Find a player across all pools, preferring an entry that actually carries
// week-by-week projections (the same person can appear in several pools).
function findWithProjections(snap, query, leagueFilter) {
  if (!normName(query)) return null;
  const lf = normName(leagueFilter || "");
  let fallback = null;
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;
    const pools = [
      ...(s.roster || []),
      ...Object.values(s.league_rosters || {}).flat(),
      ...(s.free_agents || []),
    ];
    for (const p of pools) {
      if (!nameMatches(p.name, query)) continue;
      if (p.ros_proj && Object.keys(p.ros_proj).length) return { p, league, week: s.week };
      if (!fallback) fallback = { p, league, week: s.week };
    }
  }
  return fallback;
}

// How many upcoming weeks each status zeroes out (mirrors the collector).
const _INJURY_WEEKS = { OUT: 1, SUSPENDED: 2, IR: 4, PUP: 4 };

function projectionLines(hit) {
  const p = hit.p;
  const team = p.pro_team ? ", " + p.pro_team : "";
  const header = `📅 **${p.name}** (${p.position}${team}) — rest-of-season projections (${hit.league})`;
  const ros = p.ros_proj || {};
  const weeks = Object.keys(ros).map(Number).filter((n) => !Number.isNaN(n)).sort((a, b) => a - b);
  if (!weeks.length) {
    let msg = header + "\nNo week-by-week projections are published for this player yet.";
    if (p.avg_points != null && p.avg_points > 0) msg += `\nSeason average so far: ${p.avg_points} pts/gm.`;
    return msg;
  }
  const curWeek = hit.week;
  const outWeeks = _INJURY_WEEKS[p.injury_status] || 0;  // weeks zeroed by injury
  const inOutWindow = (w) => outWeeks && w >= curWeek && w < curWeek + outWeeks;
  const lines = [header];
  if (outWeeks) {
    lines.push(outWeeks > 1
      ? `⚠️ Currently ${p.injury_status} — the next ${outWeeks} weeks are zeroed out (expected multi-week absence).`
      : `⚠️ Currently ${p.injury_status} — this week is zeroed out.`);
  }
  let sum = 0, n = 0;
  for (const w of weeks) {
    const v = ros[String(w)];
    if (typeof v === "number" && v === 0) {
      // A zero is the player's bye, unless it falls in their injury window.
      const label = inOutWindow(w) ? `(${p.injury_status.toLowerCase()})` : "(bye)";
      lines.push(`Week ${w}: — ${label}`);
    } else {
      lines.push(`Week ${w}: ${v}`);
      if (typeof v === "number") { sum += v; n++; }
    }
  }
  const avg = n ? +(sum / n).toFixed(1) : 0;
  lines.push(`—\nRemaining total: ${+sum.toFixed(1)} over ${n} game${n === 1 ? "" : "s"} (avg ${avg}/gm)`);
  lines.push("_Matchup-adjusted estimate (per-game baseline × opponent defense vs. position); not an official ESPN projection._");
  let out = lines.join("\n");
  if (out.length > 1900) out = out.slice(0, 1880) + "\n…(truncated)";
  return out;
}

// A compact view of both teams' depth by position, so the AI can weigh need.
function teamByPosition(players) {
  const by = {};
  for (const p of players || []) by[p.position] = (by[p.position] || 0) + 1;
  return Object.entries(by).sort().map(([pos, n]) => `${pos} ${n}`).join(", ") || "n/a";
}

// Find a team's roster within one platform by (fuzzy) name: the user's own team,
// or any other manager's roster in league_rosters.
function rosterOfTeam(plat, teamName) {
  if (!teamName) return null;
  const t = normName(teamName);
  if (plat.team_name && normName(plat.team_name).includes(t)) return plat.roster || [];
  for (const [team, players] of Object.entries(plat.league_rosters || {}))
    if (normName(team).includes(t)) return players || [];
  return null;
}

function rosterContext(snap, g, r, teamGive, teamGet) {
  const leagues = [...new Set([...g.found, ...r.found].map((f) => f.league))];
  if (leagues.length !== 1) return "";
  const leagueName = leagues[0];
  const plat = Object.values(snap.platforms || {}).find(
    (s) => s && s.enabled && (s.league_name || s.label) === leagueName
  );
  if (!plat) return "";

  // Give side: the named team, else the user's own team.
  const giveName = teamGive || plat.team_name;
  const giveRoster = rosterOfTeam(plat, giveName);

  // Get side: the named team, else infer from who owns the 'get' players.
  let getName = teamGet;
  let getRoster = teamGet ? rosterOfTeam(plat, teamGet) : null;
  if (!getName) {
    const partners = [...new Set(r.found.map((f) => f.owner).filter((o) => o && o !== "free agent" && o !== plat.team_name))];
    getName = partners.join(", ") || null;
    getRoster = partners.flatMap((t) => (plat.league_rosters || {})[t] || []);
  }

  const mine = !teamGive; // only call the give side "your team" when it's the default
  let ctx = `League: ${leagueName}.`;
  if (giveName && giveRoster && giveRoster.length)
    ctx += ` ${giveName}${mine ? " (your team)" : ""} roster by position: ${teamByPosition(giveRoster)}.`;
  if (getName && getRoster && getRoster.length)
    ctx += ` ${getName} roster by position: ${teamByPosition(getRoster)}.`;
  return ctx;
}

// The league's starting-lineup requirements (QB/RB/WR/TE + FLEX/SUPERFLEX).
function startingSlots(plat) {
  const s = (plat.league_settings && plat.league_settings.starters) || {};
  const slots = {
    QB: s.QB || 0, RB: s.RB || 0, WR: s.WR || 0, TE: s.TE || 0,
    FLEX: (s.FLEX || 0) + (s["RB/WR/TE"] || 0) + (s["WR/RB/TE"] || 0),
    SUPERFLEX: (s.SUPERFLEX || 0) + (s.OP || 0) + (s["QB/RB/WR/TE"] || 0),
  };
  if (!slots.QB && !slots.RB && !slots.WR && !slots.TE)
    return { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, SUPERFLEX: 0 };
  return slots;
}

// Best possible starting-lineup total from a set of {pos, pts} players: fill
// each dedicated slot with the best at that position, then FLEX/SUPERFLEX with
// the best remaining eligible. Points a bench player can't crack don't count.
function optimalStartingPoints(players, slots) {
  const by = { QB: [], RB: [], WR: [], TE: [] };
  for (const p of players) if (by[p.pos]) by[p.pos].push(p.pts);
  for (const k in by) by[k].sort((a, b) => b - a);
  const used = { QB: 0, RB: 0, WR: 0, TE: 0 };
  let total = 0;
  for (const pos of ["QB", "RB", "WR", "TE"])
    for (let i = 0; i < (slots[pos] || 0); i++)
      if (used[pos] < by[pos].length) total += by[pos][used[pos]++];
  const fill = (positions, n) => {
    for (let i = 0; i < (n || 0); i++) {
      let best = -Infinity, bp = null;
      for (const pos of positions)
        if (used[pos] < by[pos].length && by[pos][used[pos]] > best) { best = by[pos][used[pos]]; bp = pos; }
      if (bp) { total += best; used[bp]++; }
    }
  };
  fill(["RB", "WR", "TE"], slots.FLEX);
  fill(["QB", "RB", "WR", "TE"], slots.SUPERFLEX);
  return +total.toFixed(1);
}

// The remaining weeks present in the rosters' per-week projections.
function rosterWeeks(players) {
  for (const p of players || []) {
    if (p && p.ros_proj) {
      const w = Object.keys(p.ros_proj).map(Number).filter((n) => !Number.isNaN(n)).sort((a, b) => a - b);
      if (w.length) return w;
    }
  }
  return [];
}

// Week-by-week change to a team's best starting lineup if it ships `giveNames`
// and adds `getObjs` (full player objects). For EACH remaining week we rebuild
// the optimal lineup from that week's projection (byes and injury weeks = 0), so
// depth that never starts barely counts, and the timing of byes/injuries shows
// through. Returns {avg, total, weeks, worst} or null if no weekly data.
function startingDeltaWeekly(roster, giveNames, getObjs, slots, afterWeek) {
  const before = roster || [];
  const after = before.filter((p) => !giveNames.has(normName(p.name))).concat(getObjs);
  let weeks = rosterWeeks(before.concat(getObjs));
  // Only weeks still ahead: a trade can't change a week that's already locked.
  if (afterWeek) weeks = weeks.filter((w) => w > afterWeek);
  if (!weeks.length) return null;
  const wkPts = (p, w) => ({ pos: p.position, pts: (p.ros_proj && typeof p.ros_proj[w] === "number") ? p.ros_proj[w] * valueFactor(p) : 0 });
  let total = 0, worst = null;
  for (const w of weeks) {
    const d = optimalStartingPoints(after.map((p) => wkPts(p, w)), slots)
            - optimalStartingPoints(before.map((p) => wkPts(p, w)), slots);
    total += d;
    if (worst === null || d < worst.d) worst = { w: +w, d: +d.toFixed(1) };
  }
  return { avg: +(total / weeks.length).toFixed(1), total: +total.toFixed(1), weeks: weeks.length, worst };
}

// Each team's week-by-week starting-lineup change from the trade (not zero-sum:
// each team optimizes its own lineup, so both can gain). Null if we can't
// resolve both rosters in a single league.
function tradeTeamImpact(snap, g, r, teamGive, teamGet) {
  const leagues = [...new Set([...g.found, ...r.found].map((f) => f.league))];
  if (leagues.length !== 1) return null;
  const plat = Object.values(snap.platforms || {}).find(
    (s) => s && s.enabled && (s.league_name || s.label) === leagues[0]
  );
  if (!plat) return null;
  const slots = startingSlots(plat);
  const giveRoster = rosterOfTeam(plat, teamGive || plat.team_name);
  let getRoster = teamGet ? rosterOfTeam(plat, teamGet) : null;
  if (!teamGet) {
    const partners = [...new Set(r.found.map((f) => f.owner).filter((o) => o && o !== "free agent" && o !== plat.team_name))];
    getRoster = partners.flatMap((t) => (plat.league_rosters || {})[t] || []);
  }
  const giveNames = new Set(g.found.map((f) => normName(f.p.name)));
  const getNames = new Set(r.found.map((f) => normName(f.p.name)));
  const curWeek = plat.week || 0;  // exclude the current/locked week from the impact
  const giveGets = r.found.map((f) => f.p), getGets = g.found.map((f) => f.p);
  // The give side ships g and receives r; the get side the reverse.
  const give = giveRoster && giveRoster.length ? startingDeltaWeekly(giveRoster, giveNames, giveGets, slots, curWeek) : null;
  const get = getRoster && getRoster.length ? startingDeltaWeekly(getRoster, getNames, getGets, slots, curWeek) : null;
  // Fold in a little weight for team depth: the change in bench (non-starter)
  // value. Consolidating many players into one star costs depth; adding bodies
  // gains it. Starters still dominate the grade (see DEPTH_WEIGHT).
  if (give) addDepthScore(give, giveRoster, giveNames, giveGets, slots);
  if (get) addDepthScore(get, getRoster, getNames, getGets, slots);
  return { give, get };
}

// Measure the change in a roster's bench depth from the trade and fold it into
// the grade. Adds `depth` (the clamped pts/wk the trade added to or took from
// the grade for depth reasons) and `score` (the blended grade input: starting
// lineup change plus that depth nudge) to a side's weekly-impact object. The
// weight and cap are small on purpose (see TRADE_TUNING) so an uneven swap of
// bodies can't swamp the starting-lineup signal.
function addDepthScore(side, roster, giveNames, getObjs, slots) {
  const after = (roster || []).filter((p) => !giveNames.has(normName(p.name))).concat(getObjs);
  const raw = benchValue(after, slots) - benchValue(roster, slots);
  const cap = TRADE_TUNING.DEPTH_CAP;
  const depth = Math.max(-cap, Math.min(cap, +(TRADE_TUNING.DEPTH_WEIGHT * raw).toFixed(1)));
  side.depth = depth;
  side.score = +(side.avg + depth).toFixed(1);
}

// Points sitting on the bench: full roster season-avg total minus the optimal
// starting total. Higher means more quality depth to cover byes and injuries.
function benchValue(players, slots) {
  const objs = (players || []).map((p) => ({ pos: p.position, pts: (rosAvg(p) || 0) * valueFactor(p) }));
  const total = objs.reduce((a, o) => a + o.pts, 0);
  return +(total - optimalStartingPoints(objs, slots)).toFixed(1);
}

// Letter grade from a team's blended score (starting-lineup pts/wk plus the depth
// nudge). Walks the cutoffs in TRADE_TUNING, taking the first grade the score
// reaches. A positive cutoff is inclusive and a negative one exclusive, so a score
// sitting exactly on a negative boundary lands in the lower (worse) grade.
function tradeGrade(delta) {
  const cutoffs = TRADE_TUNING.GRADE_CUTOFFS;
  for (const [threshold, grade, word] of cutoffs) {
    if (threshold >= 0 ? delta >= threshold : delta > threshold) return { grade, word };
  }
  const last = cutoffs[cutoffs.length - 1];
  return { grade: last[1], word: last[2] };
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

// Replacement level on the SAME basis as tradeBasis (so value-over-replacement
// is apples-to-apples): the mean projected-per-game of the top few free agents
// at each position — i.e. the best player you could just pick up. The stored
// replacement_levels are built from weekly projections, a different scale, so we
// compute our own here and memoize it. Falls back to the weekly level if a
// position has no free-agent data.
function seasonReplacement(s) {
  if (s.__tradeRepl) return s.__tradeRepl;
  const repl = {};
  const byPos = {};
  for (const p of s.free_agents || []) {
    const b = tradeBasis(p);
    if (b != null && b > 0) (byPos[p.position] = byPos[p.position] || []).push(b);
  }
  for (const pos of ["QB", "RB", "WR", "TE"]) {
    const top = (byPos[pos] || []).sort((a, b) => b - a).slice(0, 3);
    repl[pos] = top.length
      ? +(top.reduce((a, b) => a + b, 0) / top.length).toFixed(1)
      : (s.replacement_levels || {})[pos];
  }
  s.__tradeRepl = repl;
  return repl;
}

// Find a player by name anywhere in your leagues (your roster, other teams'
// rosters, or free agents), returning the player, league, owner, and that
// league's season-average replacement levels (for value-over-replacement).
function findPlayer(snap, query, leagueFilter) {
  if (!normName(query)) return null;
  const lf = normName(leagueFilter || "");
  for (const [key, s] of Object.entries(snap.platforms || {})) {
    if (!s || !s.enabled) continue;
    const league = s.league_name || s.label || key;
    if (lf && !normName(league).includes(lf)) continue;  // scope to a league if asked
    const repl = seasonReplacement(s);
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

// Season-to-date scoring average per game (fallback basis). Falls back to this
// week's median, then the raw projection.
function seasonAvg(p) {
  if (p.avg_points != null && p.avg_points > 0) return p.avg_points;
  if (p.proj_median != null) return p.proj_median;
  if (p.proj_points != null) return p.proj_points;
  return null;
}

// Rest-of-season projection per game: the mean of the player's remaining weekly
// projections, skipping bye weeks (which project 0). Returns null if the player
// has no forecast in the snapshot (it also reports the game count).
function rosWeeks(p) {
  const ros = p.ros_proj;
  if (!ros) return [];
  return Object.values(ros).filter((v) => typeof v === "number" && v > 0);
}
function rosAvg(p) {
  const vals = rosWeeks(p);
  if (!vals.length) return null;
  return +(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2);
}

// Risk multiplier from the shape of a player's Monte Carlo range: 1.0 for a steady
// (tight-range) player, above 1 when his ceiling towers over his median (boom),
// below 1 when his floor sits well under it (bust risk). Both the upside and the
// downside are measured relative to the MEDIAN, so the factor is scale-independent
// (it reacts to how volatile a player is, not his raw score) and nets the two, then
// caps the result (see TRADE_TUNING.VALUE_BLEND). The same factor is applied
// everywhere a trade values a player, so value-over-replacement and the
// starting-lineup grade stay consistent.
function blendFactor(p) {
  const f = p.proj_floor, med = p.proj_median, c = p.proj_ceiling;
  if (med == null || !(med > 0)) return 1;
  const up = c != null ? Math.max(0, c / med - 1) : 0;    // how far the ceiling clears the median
  const down = f != null ? Math.max(0, 1 - f / med) : 0;  // how far the floor falls below it
  const b = TRADE_TUNING.VALUE_BLEND;
  const adj = b.CEILING_WEIGHT * up - b.FLOOR_WEIGHT * down;
  return 1 + Math.max(-b.CAP, Math.min(b.CAP, adj));
}

// Rest-of-season confidence multiplier for an injury designation (see
// TRADE_TUNING.INJURY_CONFIDENCE); 1 for a healthy or untagged player.
function injuryConfidence(p) {
  return TRADE_TUNING.INJURY_CONFIDENCE[(p.injury_status || "").toUpperCase()] ?? 1;
}

// The single per-player multiplier applied everywhere a trade values a player:
// the range risk blend times the injury-confidence haircut. Using one factor in
// value, replacement, and the lineup grade keeps them all consistent.
function valueFactor(p) {
  return blendFactor(p) * injuryConfidence(p);
}

// The per-game basis a trade is judged on: rest-of-season projection first
// (forward-looking is what matters for a trade), then season-to-date average, then
// the risk blend of the player's range. The mean already blends ESPN's projection,
// realized form, defense, Vegas, weather and injuries; the blend factor adds floor
// and ceiling on top. Replacement level is computed on this same basis, so
// value-over-replacement stays apples-to-apples.
function tradeBasis(p) {
  const r = rosAvg(p);
  const base = r != null ? r : seasonAvg(p);
  if (base == null) return null;
  return +(base * valueFactor(p)).toFixed(2);
}

function valueOf(found) {
  const p = found.p;
  const basis = tradeBasis(p);
  if (basis == null) return null;
  const rep = found.repl[p.position];
  return rep != null ? +(basis - rep).toFixed(1) : +Number(basis).toFixed(1);
}

function rangeStr(p) {
  const r = rosAvg(p);
  if (r != null) return `${r.toFixed(1)} proj pts/gm (${rosWeeks(p).length} games left)`;
  const a = seasonAvg(p);
  if (p.avg_points != null && p.avg_points > 0) return `${a} pts/gm this season`;
  if (a != null) return `proj ~${a}`;
  return "no projection";
}

function valStr(v) {
  return v == null ? "" : ` · value ${v > 0 ? "+" : ""}${v}`;
}

// Known injury designations, so the card can warn that a player's value assumes
// he returns. ESPN also reports ACTIVE/"" for healthy players, which we ignore.
const _INJURY_TAGS = { OUT: "OUT", DOUBTFUL: "DOUBTFUL", QUESTIONABLE: "QUESTIONABLE", IR: "IR", PUP: "PUP", SUSPENDED: "SUSP" };
function injuryTag(p) {
  return _INJURY_TAGS[(p.injury_status || "").toUpperCase()] ? ` ⚠️ ${_INJURY_TAGS[(p.injury_status || "").toUpperCase()]}` : "";
}
function isInjured(p) {
  return !!_INJURY_TAGS[(p.injury_status || "").toUpperCase()];
}

// Each player's weekly Monte Carlo floor-to-ceiling range, shown on the trade card
// so the spread the value blends in (see blendFactor / VALUE_BLEND) is visible.
// Empty when the player has no range in the snapshot.
function rangeBracket(p) {
  const f = p.proj_floor, c = p.proj_ceiling;
  if (f == null || c == null) return "";
  return ` [${f}-${c}]`;
}

// Sum value and per-game basis for one side of a trade.
function sideTotals(found) {
  let value = 0, avg = 0, hasValue = false;
  for (const f of found) {
    const v = valueOf(f);
    if (v != null) { value += v; hasValue = true; }
    const a = tradeBasis(f.p);
    if (a != null) avg += a;
  }
  return { value: +value.toFixed(1), avg: +avg.toFixed(1), hasValue };
}

function sideLines(label, side) {
  const lines = [`**${label}:**`];
  for (const f of side.found) {
    const team = f.p.pro_team ? ", " + f.p.pro_team : "";
    lines.push(`• ${f.p.name} (${f.p.position}${team})${injuryTag(f.p)} — ${rangeStr(f.p)}${rangeBracket(f.p)}${valStr(valueOf(f))}`);
  }
  for (const name of side.missing) lines.push(`• ⚠️ couldn't find "${name}"`);
  return lines;
}

function tradeVerdict(g, r, teamGive, teamGet, impact) {
  const lines = ["🔁 **Trade check**"];
  const leagues = new Set([...g.found, ...r.found].map((f) => f.league));
  if (leagues.size === 1) lines[0] += ` (${[...leagues][0]})`;

  const gt = sideTotals(g.found), rt = sideTotals(r.found);
  const sub = (t, n) => `_↳ these ${n} total ${t.value > 0 ? "+" : ""}${t.value} value (${t.avg} pts/gm)_`;
  const giveLabel = teamGive ? `📤 ${teamGive} sends away` : "📤 You send away";
  const getLabel = teamGet ? `📤 ${teamGet} sends away` : "📥 You receive";

  lines.push("");
  lines.push(...sideLines(giveLabel, g));
  if (g.found.length > 1) lines.push(sub(gt, g.found.length));
  lines.push("");
  lines.push(...sideLines(getLabel, r));
  if (r.found.length > 1) lines.push(sub(rt, r.found.length));
  lines.push("");

  const hasRange = [...g.found, ...r.found].some((f) => f.p.proj_floor != null && f.p.proj_ceiling != null);
  if (hasRange) {
    lines.push("_[floor-ceiling] is each player's weekly range (Monte Carlo); value and grade blend the mean with a little upside (ceiling) and downside (floor), on top of all the other factors._");
    lines.push("");
  }

  const hurt = [...g.found, ...r.found].filter((f) => isInjured(f.p));
  if (hurt.length) {
    const names = hurt.map((f) => `${f.p.name} (${_INJURY_TAGS[(f.p.injury_status || "").toUpperCase()]})`).join(", ");
    lines.push(`⚠️ Injury watch: ${names}. The current week is docked (zeroed if ESPN projects him out), but the rest-of-season value still assumes he returns and plays. The model can't know his timeline, so weigh this yourself.`);
    lines.push("");
  }

  if (leagues.size > 1)
    lines.push(`⚠️ Players span different leagues (${[...leagues].join(", ")}) — a trade only works within one league.`);
  if (g.missing.length || r.missing.length)
    lines.push("⚠️ Some players weren't found, so the verdict is incomplete.");

  if (gt.hasValue && rt.hasValue) {
    const diff = +(rt.value - gt.value).toFixed(1);  // raw value swing (give side net)
    const giveName = teamGive || "You";
    const getName = teamGet || "The other side";
    const sign = (x) => `${x > 0 ? "+" : ""}${x}`;

    // Grade on the week-by-week change to each team's actual STARTING lineup
    // (bench depth you wouldn't play barely counts; byes and injuries show up in
    // the specific weeks they happen), not the zero-sum raw value swing.
    const haveImpact = impact && impact.give && impact.get;
    const gD = haveImpact ? impact.give : null;
    const rD = haveImpact ? impact.get : null;
    const gGrade = tradeGrade(haveImpact ? gD.score : diff);
    const rGrade = tradeGrade(haveImpact ? rD.score : -diff);
    const depthStr = (d) => d.depth ? `, depth ${sign(d.depth)} pts/wk` : "";
    const detail = (d) => `starters ${sign(d.avg)} pts/wk (${sign(d.total)} over ${d.weeks} wk${depthStr(d)})`;
    const rough = (d) => (d.worst && d.worst.d <= -3) ? `; roughest Wk ${d.worst.w} (${d.worst.d})` : "";

    lines.push("📊 **Grades — starting lineup (week by week) + a little depth:**");
    lines.push(`• ${giveName}: **${gGrade.grade}** (${gGrade.word})${haveImpact ? ` — ${detail(gD)}${rough(gD)}` : ""}`);
    lines.push(`• ${getName}: **${rGrade.grade}** (${rGrade.word})${haveImpact ? ` — ${detail(rD)}${rough(rD)}` : ""}`);
    lines.push("");

    if (haveImpact) {
      const favored = diff >= 0 ? giveName : getName;
      lines.push(`_Graded mostly on rebuilding each team's best starting lineup for every remaining week (byes and injuries count as 0 those weeks), plus a little weight for bench depth to cover injuries. Raw value across all players (over replacement) favors ${favored} by ${Math.abs(diff)}, but the lineup you actually start week to week is what matters most. The human side is still your call._`);
    } else {
      lines.push("_Graded on value over replacement (couldn't read full rosters to grade the starting lineup here). Projections are rest-of-season points per game. The human side is still your call._");
    }
  }
  return lines.join("\n");
}

// Does the note name every traded player (checked by last name)?
function mentionsAllPlayers(note, players) {
  const n = normName(note || "");
  return players.every((f) => {
    const parts = normName(f.p.name).split(" ").filter(Boolean);
    const last = parts[parts.length - 1] || "";
    return last && n.includes(last);
  });
}

// Join names as a readable list: "A", "A and B", "A, B, and C".
function nameList(found) {
  const ns = found.map((f) => f.p.name);
  if (!ns.length) return "nobody";
  if (ns.length === 1) return ns[0];
  if (ns.length === 2) return `${ns[0]} and ${ns[1]}`;
  return `${ns.slice(0, -1).join(", ")}, and ${ns[ns.length - 1]}`;
}

// Code-written, always-correct prose version of the verdict. Because the facts
// (who adds whom, each grade, each lineup change) come straight from the
// computed numbers, this can NEVER flip a player's direction or contradict the
// grades the way a free-form AI take can. The AI take rides below it as color.
function deterministicSummary(g, r, teamGive, teamGet, impact) {
  const gt = sideTotals(g.found), rt = sideTotals(r.found);
  if (!gt.hasValue || !rt.hasValue) return "";
  const diff = +(rt.value - gt.value).toFixed(1);
  const haveImpact = impact && impact.give && impact.get;
  const gScore = haveImpact ? impact.give.score : diff;
  const rScore = haveImpact ? impact.get.score : -diff;
  const giveName = teamGive || "You";
  const getName = teamGet || "The other side";
  const sign = (x) => `${x > 0 ? "+" : ""}${x}`;
  const verb = (s) =>
    s >= 2 ? "upgrades its starting lineup"
    : s >= 0.7 ? "slightly upgrades its lineup"
    : s > -0.7 ? "comes out about even"
    : s > -2 ? "slightly downgrades its lineup"
    : "downgrades its lineup";
  // give team adds r / loses g;  get team adds g / loses r.
  const adds = nameList(r.found), loses = nameList(g.found);
  const line = (name, score, impactSide, add, lose) => {
    const grade = tradeGrade(score).grade;
    let tail = "";
    if (haveImpact && impactSide) {
      tail = ` (${sign(impactSide.avg)} starting pts/wk`
        + (impactSide.depth ? `, ${sign(impactSide.depth)} depth` : "")
        + ")";
    }
    return `• **${name}** ${verb(score)} — **${grade}**${tail}. Adds ${add}; gives up ${lose}.`;
  };
  return [
    line(giveName, gScore, haveImpact ? impact.give : null, adds, loses),
    line(getName, rScore, haveImpact ? impact.get : null, loses, adds),
  ].join("\n");
}

async function tradeCommentary(g, r, context, env, teamGive, teamGet, impact) {
  if (!env.OPENROUTER_API_KEY || (!g.found.length && !r.found.length)) return "";
  const desc = (f) => {
    const a = rosAvg(f.p);
    return `${f.p.name} (${f.p.position}${a != null ? `, ${a.toFixed(1)} proj pts/gm` : ""})`;
  };
  const all = [...g.found, ...r.found];
  const total = all.length;
  // Grades for each side, so the take agrees with the card.
  const gt = sideTotals(g.found), rt = sideTotals(r.found);
  const diff = +(rt.value - gt.value).toFixed(1);
  const haveImpact = impact && impact.give && impact.get;
  const gAvg = haveImpact ? impact.give.score : diff;
  const rAvg = haveImpact ? impact.get.score : -diff;
  const gGrade = tradeGrade(gAvg), rGrade = tradeGrade(rAvg);
  const sign = (x) => `${x > 0 ? "+" : ""}${x}`;
  const sideA = teamGive || "Team A (my team)";
  const sideB = teamGet || "Team B (the other team)";
  const givePlayers = g.found.map(desc).join("; ") || "nobody";
  const getPlayers = r.found.map(desc).join("; ") || "nobody";
  // Team-centric facts: each team's OWN receives and gives-up, with its grade,
  // so the model can't attribute a player to the wrong team or direction.
  const facts =
    `${sideA}: GRADE ${gGrade.grade} (${gGrade.word}, ${sign(gAvg)} starting pts/wk). ` +
    `RECEIVES (additions): ${getPlayers}. GIVES UP (losses): ${givePlayers}.\n` +
    `${sideB}: GRADE ${rGrade.grade} (${rGrade.word}, ${sign(rAvg)} starting pts/wk). ` +
    `RECEIVES (additions): ${givePlayers}. GIVES UP (losses): ${getPlayers}.\n` +
    (context || "");
  const length = total <= 2 ? "two tight sentences" : "three tight sentences (about 70 words max)";
  const ask = (stern) => askOpenRouter(
    (stern ? "IMPORTANT: your previous answer left out a player or got a player's direction wrong. " : "") +
    `Give a concise take on this fantasy trade for the rest of the season, in ${length}. Name every player once. ` +
    "For EACH team, use ONLY its own RECEIVES and GIVES UP lists: a player in a team's RECEIVES list is an " +
    "ADDITION for that team, and a player in its GIVES UP list is a LOSS for that team. Never describe a " +
    "player a team receives as a loss, or one it gives up as a gain, and never attribute a player to the " +
    "wrong team. Your take MUST match each team's GRADE: a B or A (positive) means the trade HELPS that team, " +
    "so do not call it a loss or a bloat move; a D or F (negative) means it hurts them. Judge by the starting " +
    "lineup (who actually plays), not raw roster counts, and explain plainly why each team earns its grade.",
    facts, env
  );
  let note = await ask(false);
  // Safety net: if the model dropped a player, re-ask once, more sternly.
  if (note && total > 2 && !mentionsAllPlayers(note, all)) {
    const retry = await ask(true).catch(() => "");
    if (retry && mentionsAllPlayers(retry, all)) note = retry;
  }
  return note;
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

// Send an extra message after the first reply (its own 2000-char budget).
async function sendFollowup(interaction, env, text) {
  if (!text) return;
  const url = `https://discord.com/api/v10/webhooks/${env.DISCORD_APP_ID}/${interaction.token}`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text.slice(0, 1900) }),
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

  const messages = [
    { role: "system", content: system },
    { role: "user", content: question },
  ];
  const call = async (extra) => {
    const resp = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
        "X-Title": "Fantasy Football Monitor",
      },
      body: JSON.stringify({
        model: env.OPENROUTER_MODEL || DEFAULT_MODEL,
        messages,
        max_tokens: 2048,  // room for the answer even if the model reasons first
        temperature: 0.4,
        ...extra,
      }),
    });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new Error(`OpenRouter ${resp.status}: ${body.slice(0, 180)}`);
    }
    const data = await resp.json();
    const m = data.choices?.[0]?.message || {};
    return (m.content || m.reasoning || "").trim();
  };
  // First try with reasoning off (cheaper and direct). Some models reject that
  // or spend the whole budget thinking and return empty text, so on an error or
  // an empty answer, retry letting the model reason normally.
  try {
    const out = await call({ reasoning: { enabled: false } });
    if (out) return out;
  } catch (_) { /* fall through to the default-reasoning retry */ }
  return await call({});
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
