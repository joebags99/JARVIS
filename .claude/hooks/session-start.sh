#!/bin/bash
# JARVIS SessionStart hook — installs the dev tooling (ruff + pytest + the light
# runtime deps the unit tests import) so `ruff check .` and `pytest` work in a
# fresh Claude Code on the web session. Deliberately skips the full
# requirements.txt: the heavy native deps the desktop app needs (pywebview,
# faster-whisper, …) aren't required by the unit tests, so this stays fast.
#
# Synchronous: the session waits for this to finish, guaranteeing the tooling is
# ready before the agent runs anything. Idempotent — safe to run every start.
set -euo pipefail

# Only run in remote (Claude Code on the web) environments.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
python3 -m pip install --quiet -r requirements-dev.txt
echo "JARVIS dev tooling ready (ruff + pytest)."
