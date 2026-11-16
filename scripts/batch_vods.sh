#!/usr/bin/env bash
# Usage: scripts/batch_vods.sh streamer:vodid [...]   (runs 3 at a time, top 3 each, no judge)
cd "$(dirname "$0")/.." || exit 1
mkdir -p work/logs
run() {
  s=${1%%:*}; v=${1##*:}
  mkdir -p "work/$v"
  [ -s "work/$v/chat.json" ] || bin/TwitchDownloaderCLI chatdownload --id "$v" -o "work/$v/chat.json" --collision Overwrite >/dev/null 2>&1
  .venv/bin/python -m clipwatcher.vod --vod "$v" --streamer "$s" --chat "work/$v/chat.json" --top "${TOP:-3}" \
    --no-judge --work work --out staging > "work/logs/$s.log" 2>&1
  echo "$s $v exit=$? $(grep -c staged "work/logs/$s.log") staged"
}
export -f run
printf '%s\n' "$@" | xargs -P 3 -I{} bash -c 'run {}'
