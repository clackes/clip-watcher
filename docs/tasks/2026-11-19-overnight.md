# Overnight task, 19 Nov 2026 (owner)

Goal: by morning the bot runs on the new server and Telegram already holds finished clips waiting for approval.

1. **Migrate** to the new Windows server 140.82.40.80 (WinRM, password in Keychain
   `clipwatcher-winrm-140.82.40.80`). Same setup as the old box: code, venv, `.env`, streamers/operators,
   tests green, background task `cw-live`. Stop the old bot first: only one Telegram poller per token.
2. **Publishing only after approval.** No auto-approve. When an operator taps Approve in Telegram the
   Short goes **public** on King Capybara (@capybarasking), uploaded with the Chrome profile the owner
   signed in to on the new server.
3. **Telegram bot, plainer copy.** Short sentences anyone gets at a glance, a few emoji. New **🔁 Redo**
   button: the operator may add an optional note ("start earlier", "cut the ending"), and the clip is cut,
   judged and rendered again with that note.
4. **Captions** like the reference Short (youtube.com/shorts/K1LR1iY9WS0): one word at a time on the seam
   between webcam and gameplay, bold condensed caps, white, key words yellow, thick black outline,
   light shadow, small pop-in. Size as in the reference (~64 px caps on 1080).
5. **Chat calibration per streamer.** Every chat behaves differently and changes over the stream. Learn
   each chat before firing: a warm-up period, a baseline primed from the chat already logged (so a restart
   does not re-learn from zero), and spikes judged against that streamer's recent behaviour.
6. **New brand**: the CapybarasKing logo (owner's image) replaces the old banner in the Short and becomes
   the YouTube channel banner.
7. **Archive the channel**: set every video already on King Capybara to Private (never delete), so
   approved clips post as on a fresh channel.
