# Discord `/ask` bot — setup

Ask about your leagues right inside Discord (`/ask question: ...`) and get the
answer back in the same channel. It runs on a free **Cloudflare Worker** and
uses your **OpenRouter** key.

You'll do this once. Takes ~30 minutes. Go in order.

---

## What you need
- A **Discord server** you own (or can add a bot to).
- A free **Cloudflare** account.
- **Node 18+** and **npm** on your computer (for `wrangler` + registering the command).
- Your **OpenRouter API key**.

---

## Step 1 — Create the Discord application
1. Go to <https://discord.com/developers/applications> → **New Application**. Name it (e.g. "FF Assistant").
2. On the **General Information** page, copy:
   - **Application ID**  → this is `DISCORD_APP_ID`
   - **Public Key**      → this is `DISCORD_PUBLIC_KEY`
3. Left sidebar → **Bot** → **Add Bot** → **Reset Token** → copy the **Bot Token** (this is `DISCORD_BOT_TOKEN`). Keep it secret.
4. Left sidebar → **OAuth2 → URL Generator**: tick **`applications.commands`** and **`bot`** scopes, then open the generated URL and add the bot to your server.

## Step 2 — Deploy the Cloudflare Worker
From this `discord-bot/` folder:
```bash
npm install -g wrangler
wrangler login                       # opens a browser to your Cloudflare account
```
Edit **`wrangler.toml`** and fill in `DISCORD_APP_ID` and `DISCORD_PUBLIC_KEY`
(from Step 1). Then set your secrets and deploy:
```bash
wrangler secret put OPENROUTER_API_KEY     # paste your OpenRouter key
wrangler secret put GITHUB_TOKEN           # optional — see Step 4
wrangler deploy
```
`wrangler deploy` prints your Worker URL, e.g.
`https://ff-ask-bot.<you>.workers.dev`. Copy it.

## Step 3 — Point Discord at the Worker
1. Back in the Discord Developer portal → your app → **General Information**.
2. Set **Interactions Endpoint URL** to your Worker URL → **Save Changes**.
   - Discord sends a test PING; if it saves without error, the signature check works. ✅

## Step 4 — (Optional) Ground answers in your live data
Without this, the bot still answers, but it can't see your rosters. To let it
read your latest snapshot:
1. Create a **fine-grained GitHub token**: <https://github.com/settings/tokens?type=beta>
   - **Repository access:** only `fantasy-football-monitor`
   - **Permissions → Repository → Contents:** **Read-only**
2. `wrangler secret put GITHUB_TOKEN` (paste it), then `wrangler deploy` again.
   (`GITHUB_REPO` is already set in `wrangler.toml`.)

## Step 5 — Register the `/ask` command
Find your **server (guild) ID**: in Discord, enable *Developer Mode*
(User Settings → Advanced), then right-click your server → **Copy Server ID**.
```bash
DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... node register.js
```
A guild command appears instantly. (Omit `DISCORD_GUILD_ID` for a global command
— works everywhere but can take up to ~1 hour to show up.)

## Step 6 — Ask!
In your server, type:
```
/ask question: Start Odunze or Fannin in the BBL this week?
```
Within a few seconds the bot replies in the channel.

---

## Troubleshooting
- **Discord won't save the endpoint URL** → `DISCORD_PUBLIC_KEY` in `wrangler.toml`
  is wrong, or you didn't `wrangler deploy` after editing it.
- **"couldn't answer that … OpenRouter 401"** → `OPENROUTER_API_KEY` secret is wrong.
- **OpenRouter 402 / no credits** → set `OPENROUTER_MODEL` in `wrangler.toml` to a
  free model like `meta-llama/llama-3.1-8b-instruct:free`, then `wrangler deploy`.
- **Answers ignore your teams** → Step 4 not done, or the token lacks Contents:read.
- **`/ask` doesn't appear** → re-run Step 5 with a `DISCORD_GUILD_ID`, and make sure
  the bot was added to the server (Step 1.4).

## How it works
`worker.js` verifies Discord's signature (WebCrypto Ed25519, no dependencies),
answers the validation PING, and for `/ask` returns a *deferred* reply, then
fetches your snapshot, calls OpenRouter, and edits in the answer. Same context
and prompt as the local `python -m ffmonitor.ask` command — just a different
front door.
