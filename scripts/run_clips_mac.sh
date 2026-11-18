#!/usr/bin/env bash
# Run the clips lane on this Mac: every hour, viewers' top clips (Twitch + Kick) -> judge -> Shorts -> Telegram.
#   scripts/run_clips_mac.sh                 # every English streamer in streamers.yaml
#   scripts/run_clips_mac.sh adinross,n3on   # a subset
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
export PATH="$PWD/.venv/bin:$PATH"
mkdir -p work/logs
exec caffeinate -is .venv/bin/python -m clipwatcher.clipslane ${1:+--streamers "$1"} 2>&1 | tee -a "work/logs/clips-$(date +%Y%m%d).log"
