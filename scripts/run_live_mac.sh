#!/usr/bin/env bash
# Run the bot on live streams on this Mac: the Telegram approval bot (Claim/Approve buttons) plus the live
# lane (record, chat, detect, judge, render, stage). No Twitch/Kick credentials needed: anonymous IRC chat,
# Kick's public chat history and anonymous streamlink, which waits on offline channels until they go live.
#   scripts/run_live_mac.sh                  # every English streamer in config/streamers.yaml
#   scripts/run_live_mac.sh kaicenat,clix    # a subset
# Ctrl-C (or closing the terminal) stops both. caffeinate keeps the Mac awake while it runs.
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
export PATH="$PWD/.venv/bin:$PATH"
mkdir -p work/logs
day=$(date +%Y%m%d)

bot_pid=""
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  if pgrep -f "clipwatcher.queue.telegram_bot" >/dev/null; then
    echo "telegram bot already running"
  else
    .venv/bin/python -m clipwatcher.queue.telegram_bot >>"work/logs/telegram-$day.log" 2>&1 &
    bot_pid=$!
    echo "telegram bot started (pid $bot_pid, log work/logs/telegram-$day.log)"
  fi
else
  echo "TELEGRAM_BOT_TOKEN not set: clips are staged in staging/ without approval cards"
fi
trap '[ -n "$bot_pid" ] && kill "$bot_pid" 2>/dev/null' EXIT INT TERM

caffeinate -is .venv/bin/python -m clipwatcher.live ${1:+--streamers "$1"} 2>&1 | tee -a "work/logs/live-$day.log"
