#!/bin/bash
# One-time setup for a Meeseeks box. Safe to run again: it only fills in what is missing.
#   ./setup.sh                          asks for the model server, key and model
#   ./setup.sh URL [MODEL [KEY]]        takes them as arguments
set -e
BOX="$(cd "$(dirname "$0")" && pwd)"; cd "$BOX"
QWEN_CODE_VERSION=0.24.3

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "checking tools"
command -v python3 >/dev/null || die "python3 is required"
command -v node >/dev/null    || die "Node.js 22 or newer is required (https://nodejs.org)"
command -v npm >/dev/null     || die "npm is required (it comes with Node.js)"
command -v curl >/dev/null    || die "curl is required"
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
[ "$NODE_MAJOR" -ge 22 ] || die "Node.js 22 or newer is required (you have $(node -v))"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"

if [ ! -x venv/bin/python ]; then
  say "python environment (Flask)"
  python3 -m venv venv || die "python3 -m venv failed (on Debian/Ubuntu: sudo apt install python3-venv)"
  venv/bin/pip install -q --upgrade pip
  venv/bin/pip install -q flask
fi

if [ ! -f vendor/node_modules/@qwen-code/qwen-code/cli-entry.js ]; then
  say "installing Qwen Code $QWEN_CODE_VERSION (the agent)"
  npm install --prefix vendor --no-audit --no-fund --loglevel=error \
      "@qwen-code/qwen-code@$QWEN_CODE_VERSION"
fi

if [ ! -f box.env ]; then
  # Take what the arguments give; ask for the rest.
  URL="${1:-}"; MODEL="${2:-}"; KEY="${3:-}"
  if [ -z "$URL" ]; then
    read -r -p "Model server URL (OpenAI-compatible) [http://localhost:8000/v1]: " URL || true
    URL="${URL:-http://localhost:8000/v1}"
  fi
  if [ -z "$KEY" ]; then
    read -r -p "API key (press Enter if your server needs none): " KEY || true
    KEY="${KEY:-none}"
  fi
  if [ -z "$MODEL" ]; then
    # Offer the first model the server lists.
    FIRST=$(curl -sf -m 10 -H "Authorization: Bearer $KEY" "${URL%/}/models" 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || true)
    read -r -p "Model name${FIRST:+ [$FIRST]}: " MODEL || true
    MODEL="${MODEL:-$FIRST}"
  fi
  [ -n "$MODEL" ] || die "no model name given"
  umask 077
  sed -e "s|^OPENAI_BASE_URL=.*|OPENAI_BASE_URL=$URL|" \
      -e "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$KEY|" \
      -e "s|^OPENAI_MODEL=.*|OPENAI_MODEL=$MODEL|" box.env.example > box.env
  umask 022
  say "wrote box.env"
fi

if [ ! -s .token ]; then
  ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > .token )
  say "made a new access key for the box page (.token)"
fi

# The agent's own home: its settings, memory and chats live here, never in yours.
mkdir -p home/.qwen run
[ -f home/QWEN.md ] || cp template/QWEN.md home/QWEN.md
chmod +x bin/qwen-local start.sh stop.sh

say "checking the model"
set -a; . ./box.env; set +a
# The agent's settings name the model from box.env (kept in step on every run).
[ -f home/.qwen/settings.json ] || cp template/settings.json home/.qwen/settings.json
python3 - home/.qwen/settings.json "$OPENAI_MODEL" <<'PY'
import json, sys
p, model = sys.argv[1], sys.argv[2]
d = json.load(open(p)); d.setdefault("model", {})["name"] = model
open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY
if curl -sf -m 15 -H "Authorization: Bearer $OPENAI_API_KEY" "${OPENAI_BASE_URL%/}/models" >/dev/null; then
  echo "model answers at $OPENAI_BASE_URL"
else
  echo "WARNING: $OPENAI_BASE_URL did not answer with that key - check OPENAI_API_KEY in box.env"
fi

say "done - start the box with ./start.sh"
