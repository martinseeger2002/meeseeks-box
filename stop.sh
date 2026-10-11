#!/bin/bash
# Stop the box: the page, the agent, and every command the agent started.
BOX="$(cd "$(dirname "$0")" && pwd)"; cd "$BOX"
if [ ! -f run/box.pid ] || ! kill -0 "$(cat run/box.pid)" 2>/dev/null; then
  echo "The box is not running."; rm -f run/box.pid; exit 0
fi
PID=$(cat run/box.pid)
# The relay starts the agent in its own process group; take those down too.
for child in $(pgrep -P "$PID"); do kill -- -"$(ps -o pgid= "$child" | tr -d ' ')" 2>/dev/null; done
kill "$PID" 2>/dev/null
for _ in $(seq 10); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
kill -9 "$PID" 2>/dev/null
rm -f run/box.pid
echo "Box stopped."
