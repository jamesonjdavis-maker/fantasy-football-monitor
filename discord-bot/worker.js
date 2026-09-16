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
      const opt = (interaction.data.options || []).find((o) => o.name === "question");
      const question = (opt && opt.value ? String(opt.value) : "").trim();
      if (!question) {
        return json({ type: 4, data: { content: "Ask me something with `/ask question:...`" } });
      }
      // Defer, then finish the work in the background.
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
  // Edit the deferred reply with the final answer (Discord content cap ~2000).
  const url = `https://discord.com/api/v10/webhooks/${env.DISCORD_APP_ID}/${interaction.token}/messages/@original`;
  await fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text.slice(0, 1900) || "(no answer)" }),
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
async function buildContext(env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return "";
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
  if (!resp.ok) return "";
  const snap = await resp.json();
  return summarize(snap);
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
