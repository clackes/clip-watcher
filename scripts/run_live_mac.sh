#!/usr/bin/env bash
# Run the live fast lane on this Mac and keep it running (caffeinate stops sleep while it runs).
#   scripts/run_live_mac.sh kaicenat,clix
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
export PATH="$PWD/.venv/bin:$PATH"
mkdir -p work/logs
exec caffeinate -is .venv/bin/python -m clipwatcher.live ${1:+--streamers "$1"} 2>&1 | tee -a "work/logs/live-$(date +%Y%m%d).log"
