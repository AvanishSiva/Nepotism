#!/bin/bash
# Start your REAL Chrome with remote debugging so job-agent can attach (spec §9).
# Uses a dedicated profile dir (newer Chrome blocks remote debugging on the default
# profile). First time: log in to LinkedIn in the window that opens, then leave it open.
# Your normal Chrome can keep running alongside; this is a separate profile.
set -euo pipefail
PROFILE_DIR="${JOB_AGENT_CHROME_PROFILE:-$HOME/.job-agent-chrome}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null; then
  echo "Chrome already listening on 127.0.0.1:9222"
  exit 0
fi
mkdir -p "$PROFILE_DIR"
"$CHROME" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE_DIR" --no-first-run --no-default-browser-check \
  "https://www.linkedin.com/feed/" >/dev/null 2>&1 &
for _ in $(seq 1 20); do
  sleep 0.5
  if curl -s -m 1 http://127.0.0.1:9222/json/version >/dev/null; then
    echo "Chrome started (profile: $PROFILE_DIR). Log in to LinkedIn there if you haven't."
    exit 0
  fi
done
echo "Chrome did not open the debug port. Is another Chrome using $PROFILE_DIR?" >&2
exit 1
