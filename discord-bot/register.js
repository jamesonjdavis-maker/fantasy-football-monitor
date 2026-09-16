/**
 * One-time: register the `/ask` slash command with Discord.
 *
 * Run with Node 18+ (has global fetch):
 *   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... [DISCORD_GUILD_ID=...] node register.js
 *
 * Set DISCORD_GUILD_ID to your server's ID to register a *guild* command
 * (appears instantly — best for testing). Omit it to register a *global*
 * command (works everywhere, but can take up to ~1 hour to show up).
 */

const APP_ID = process.env.DISCORD_APP_ID;
const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN;
const GUILD_ID = process.env.DISCORD_GUILD_ID;

if (!APP_ID || !BOT_TOKEN) {
  console.error("Set DISCORD_APP_ID and DISCORD_BOT_TOKEN environment variables.");
  process.exit(1);
}

const command = {
  name: "ask",
  description: "Ask about your fantasy football leagues (start/sit, waivers, matchups)",
  options: [
    {
      name: "question",
      description: "What do you want to know?",
      type: 3, // STRING
      required: true,
    },
  ],
};

const url = GUILD_ID
  ? `https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands`
  : `https://discord.com/api/v10/applications/${APP_ID}/commands`;

(async () => {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bot ${BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(command),
  });

  const text = await res.text();
  console.log(`HTTP ${res.status}`);
  console.log(text);
  if (res.ok) {
    console.log(GUILD_ID
      ? "✅ Registered guild command (instant)."
      : "✅ Registered global command (may take up to ~1h).");
  } else {
    console.log("❌ Registration failed. 401 = bad bot token; 403 'Missing " +
      "Access' = re-add the bot to the server with the applications.commands scope.");
  }
})();
