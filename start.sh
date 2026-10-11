#!/bin/bash
# Start the box in the background. There is only ever ONE: if it is already running,
# this just prints its link again.
BOX="$(cd "$(dirname "$0")" && pwd)"; cd "$BOX"
[ -f box.env ] && [ -s .token ] && [ -x venv/bin/python ] || { echo "run ./setup.sh first"; exit 1; }
set -a; . ./box.env; set +a
mkdir -p run
# Keep the agent's model in step with box.env.
python3 - home/.qwen/settings.json "$OPENAI_MODEL" <<'PY'
import json, sys
p, model = sys.argv[1], sys.argv[2]
d = json.load(open(p)); d.setdefault("model", {})["name"] = model
open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY
LINK="http://localhost:${PORT}/?k=$(cat .token)"

if [ -f run/box.pid ] && kill -0 "$(cat run/box.pid)" 2>/dev/null; then
  echo "The box is already running (pid $(cat run/box.pid))."
  echo "Open: $LINK"
  exit 0
fi
if curl -s -o /dev/null -m 2 "http://127.0.0.1:${PORT}/healthz"; then
  echo "Something else is already using port $PORT - change PORT in box.env."
  exit 1
fi

# The agent gets its own HOME (./home) so it never touches your own ~/.qwen.
HOME="$BOX/home" nohup venv/bin/python relay/serve.py \
    --port="$PORT" --token-file="$BOX/.token" --title="$TITLE" \
    --terms --mode=default --no-desktop \
    >> run/box.log 2>&1 < /dev/null &
echo $! > run/box.pid

for _ in $(seq 20); do
  curl -s -o /dev/null -m 1 "http://127.0.0.1:${PORT}/healthz" && break
  sleep 0.5
done
if kill -0 "$(cat run/box.pid)" 2>/dev/null; then
  echo "Mr. Meeseeks is in the box."
  echo "Open: $LINK"
else
  echo "The box did not start - see run/box.log"; rm -f run/box.pid; exit 1
fi
