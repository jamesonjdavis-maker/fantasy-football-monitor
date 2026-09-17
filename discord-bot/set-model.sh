#!/usr/bin/env bash
# Change the OpenRouter model the Discord /ask bot uses, then redeploy in one step.
#
# Usage:  ./set-model.sh <model-id>
#   e.g.  ./set-model.sh anthropic/claude-3.5-sonnet
#
# Find model IDs at https://openrouter.ai/models (copy the id exactly).
set -euo pipefail

# Always operate on this script's own folder, wherever you run it from.
cd "$(dirname "$0")"

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
  echo "Usage: ./set-model.sh <model-id>"
  echo
  echo "Find model IDs at https://openrouter.ai/models. Examples:"
  echo "  openai/gpt-4o-mini                       cheap (current default)"
  echo "  anthropic/claude-3.5-haiku               better, still cheap"
  echo "  anthropic/claude-3.5-sonnet              best reasoning"
  echo "  openai/gpt-4o                            top quality"
  echo "  meta-llama/llama-3.1-8b-instruct:free    free"
  exit 1
fi

# Rewrite the OPENROUTER_MODEL line in wrangler.toml.
tmp="$(mktemp)"
found=0
while IFS= read -r line; do
  if [[ "$line" == OPENROUTER_MODEL* ]]; then
    echo "OPENROUTER_MODEL = \"$MODEL\""
    found=1
  else
    echo "$line"
  fi
done < wrangler.toml > "$tmp"
mv "$tmp" wrangler.toml

if [ "$found" -eq 0 ]; then
  echo "Error: couldn't find an OPENROUTER_MODEL line in wrangler.toml." >&2
  exit 1
fi

echo "Model set to: $MODEL"
echo "Deploying to Cloudflare..."
wrangler deploy
echo "Done. Your Discord /ask bot now uses $MODEL."
