#!/usr/bin/env bash
# ThroughLog — one-step bootstrap (macOS / Linux).
#
# Verifies Python >= 3.12, creates a local venv, installs ThroughLog with the capture
# extras, seeds config.json from the example, and launches the app (`tl up`).
# Re-running is safe: an existing venv/config is reused, never clobbered.
#
#   ./scripts/bootstrap.sh            # set up and launch
#   ./scripts/bootstrap.sh --no-launch
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

no_launch=0
[ "${1:-}" = "--no-launch" ] && no_launch=1

find_python() {
  for cand in python3.12 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
        echo "$cand"; return 0
      fi
    fi
  done
  return 1
}

py="$(find_python || true)"
if [ -z "$py" ]; then
  echo "[tl] Python 3.12+ not found. Install it and re-run." >&2
  exit 1
fi
echo "[tl] using Python: $py"

if [ ! -x "venv/bin/python" ]; then
  echo "[tl] creating venv ..."
  "$py" -m venv venv
fi
vpy="venv/bin/python"

echo "[tl] installing ThroughLog + capture extras (this can take a minute) ..."
"$vpy" -m pip install --upgrade pip >/dev/null
"$vpy" -m pip install -e ".[capture]"

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[tl] created config.json (use the in-app Settings to add your API key)."
fi

if [ "$no_launch" = "1" ]; then
  echo "[tl] setup complete. Start the app with:  venv/bin/python -m throughlog.cli up"
  exit 0
fi

echo "[tl] launching the app (Ctrl+C to stop) ..."
exec "$vpy" -m throughlog.cli up
