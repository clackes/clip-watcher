"""Plain words for what a run reports: the page tells a person what is happening, never a log line.

A lane reports its progress as events (clipwatcher.events) and prints log lines for whoever debugs it. Here
both become sentences a person who has never opened a terminal can follow:

    say(event)      one line of the run's story for an event, or None when it says nothing new
    problem(error)  an error text as what went wrong and, where there is one, what to do about it
    explain(line)   a log line a person should hear about, as a sentence, or None (it stays technical)
    dress(event)    the event as the page gets it: with "say" and, for a failure, "problem"

Nothing is lost: the lines as the lane printed them stay in the run's technical log, one click away.
"""

from __future__ import annotations

import re
from typing import Any

# step -> (its name in the list of steps, what is going on while it runs, what was done)
STEPS: dict[str, tuple[str, str, str]] = {
    "wait": ("Let the moment finish", "Letting the moment play out", "The moment has played out"),
    "chat": ("Read the chat", "Reading the chat of the broadcast", "Read the chat of the broadcast"),
    "cut": ("Cut the moment out", "Cutting the moment out of the recording", "Cut the moment out"),
    "loudness": ("Listen for the loud part", "Listening for how loud it got", "Measured how loud it got"),
    "transcribe": ("Write down what was said", "Writing down what was said", "Wrote down what was said"),
    "screen": (
        "Quick look at the words",
        "Taking a quick look at the words",
        "Took a quick look at the words",
    ),
    "judge": ("Ask the judge", "Asking the judge if it is worth posting", "The judge had a look"),
    "layout": ("Find the webcam", "Looking for the webcam in the picture", "Looked for the webcam"),
    "render_short": ("Make the vertical Short", "Making the vertical Short", "Made the vertical Short"),
    "render_clean": (
        "Make the widescreen copy",
        "Making the clean widescreen copy",
        "Made the widescreen copy",
    ),
    "stage": ("Queue it for approval", "Putting it in the approval queue", "Put it in the approval queue"),
}
STEP_NAMES = {k: v[0] for k, v in STEPS.items()}
# a step that is over in an instant did not do its work (no speech model, no judge, no detector): what to
# say then, so the story never claims "wrote down what was said" beside "there is no speech model"
SKIPPED = {
    "wait": "",  # nothing to say: the line that announced the step is taken back
    "transcribe": "Nothing was written down.",
    "screen": "",
    "judge": "",  # the line about who wrote the title follows and says it
    "layout": "The layout set for the streamer is used.",
}
PLATFORMS = {"twitch": "Twitch", "kick": "Kick"}
FLAGS = {
    "sp": "it gives away a spoiler",
    "mu": "there is music that could be claimed",
    "gb": "it shows gambling",
}

# what went wrong -> (what to tell a person, what they can do); the first that matches is used
_PROBLEMS: tuple[tuple[str, str, str], ...] = (
    (
        r"TwitchDownloaderCLI",
        "The tool that downloads Twitch broadcasts is not installed on this machine.",
        "Kick broadcasts and live streams work without it.",
    ),
    (
        r"No space left on device|Errno 28|disk (is )?full",
        "The disk is full.",
        "Free some space, then run it again.",
    ),
    (
        r"disk.*(low|free)|free space",
        "The disk is almost full, so recording paused.",
        "Free some space and it carries on by itself.",
    ),
    (
        r"No playable streams|is offline|not live|stream (has )?ended",
        "The stream is not live right now.",
        "Pick a stream that is live.",
    ),
    (
        r"Kick VOD .* not found|No such (vod|video)|HTTP Error 404|404 Client Error|\b404\b.*Not Found",
        "That broadcast could not be found.",
        "Check the link and whose broadcast it is.",
    ),
    (
        r"\b429\b|Too Many Requests|rate.?limit",
        "The platform asked us to slow down.",
        "Nothing to do: it is tried again by itself.",
    ),
    (
        r"faster.whisper|No module named 'faster_whisper'",
        "There is no speech model on this machine, so there are no captions.",
        "",
    ),
    (
        r"No module named 'anthropic'",
        "The judge is not installed on this machine, so the built-in rules wrote the title.",
        "",
    ),
    (
        r"AuthenticationError|invalid x-api-key|authentication_error|\b401\b",
        "The judge's key was not accepted.",
        "Check the key in Connect.",
    ),
    (
        r"PermissionDeniedError|permission_error|\b403\b",
        "The judge's key may not use the model the judge is set to.",
        "Check the key in Connect.",
    ),
    (
        r"overloaded|\b529\b|\b503\b|Service Unavailable",
        "The service is busy right now.",
        "Try again in a minute.",
    ),
    (
        r"RemotionError|remotion|Remotion overlay",
        "The animated captions could not be drawn, so the built-in style was used.",
        "",
    ),
    (
        r"is [A-Z]+, not READY|not READY|already (approved|published|rejected|claimed)",
        "This clip was already decided, so its video stays as it is.",
        "",
    ),
    (r"before recording start", "The moment happened before the recording began.", ""),
    (
        r"timed out|TimeoutError|Timeout\b|ReadTimeout",
        "It took too long to answer.",
        "Try again; if it keeps happening, check the internet connection.",
    ),
    (
        r"ConnectionError|URLError|getaddrinfo|Connection (reset|refused|aborted)|Network is unreachable"
        r"|SSLError|RemoteDisconnected|Temporary failure in name resolution|nodename nor servname",
        "The internet connection dropped.",
        "Check the connection, then run it again.",
    ),
    (
        r"ffmpeg|ffprobe|Invalid data found|moov atom|CalledProcessError",
        "The video could not be processed.",
        "Run it again; if it keeps happening, that recording is damaged.",
    ),
    (r"Permission denied|Errno 13", "A file could not be written to this machine.", ""),
    (r"KeyboardInterrupt", "It was stopped.", ""),
)
_COMPILED = tuple((re.compile(p, re.I), text, fix) for p, text, fix in _PROBLEMS)
UNKNOWN = "Something went wrong here."
UNKNOWN_FIX = "The technical details below say exactly what."


def problem(error: Any) -> dict[str, Any]:
    """An error text in plain words: {"text", "fix", "known"}. An error nobody wrote a sentence for is said
    to be one, and where to read it."""
    raw = str(error or "")
    for rx, text, fix in _COMPILED:
        if rx.search(raw):
            return {"text": text, "fix": fix, "known": True}
    return {"text": UNKNOWN, "fix": UNKNOWN_FIX, "known": False}


def rejection(reason: Any) -> str:
    """Why a clip was not kept. The judge's own sentence is already written for people; its flags are not."""
    raw = str(reason or "").strip()
    if raw.lower().startswith("flags:"):
        why = [FLAGS.get(f.strip(), f.strip()) for f in raw.split(":", 1)[1].split(",") if f.strip()]
        return "The judge passed on it: " + " and ".join(why) + "." if why else "The judge passed on it."
    if not raw:
        return "The judge passed on it."
    known = problem(raw)
    if known["known"]:
        return known["text"]
    return "The judge passed on it: " + raw.rstrip(".") + "."


def _took(seconds: Any) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 1:
        return ""
    if s < 90:
        return f" ({s:.0f} s)"
    return f" ({int(s // 60)} min {int(s % 60)} s)"


def _hours(value: Any) -> str:
    try:
        h = float(value)
    except (TypeError, ValueError):
        return "the broadcast so far"
    if h < 1:
        return f"the last {max(1, round(h * 60))} minutes"
    return f"{h:.1f}".rstrip("0").rstrip(".") + (" hour" if round(h, 1) == 1 else " hours")


def _n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or 0)


def _line(text: str, tone: str = "info", key: str | None = None) -> dict[str, Any]:
    return {"text": text, "tone": tone, "key": key}


def say(event: dict[str, Any]) -> dict[str, Any] | None:
    """One line of a run's story: {"text", "tone": info|good|warn|bad, "key"}. Lines with the same key are
    one line that changes (a step that starts and ends, the count of messages read): the page keeps the
    newest, and takes the line back when "drop" is set. None: this event tells a person nothing new."""
    kind = event.get("kind")
    who = str(event.get("streamer") or "the streamer")
    if kind == "start":
        p = event.get("params") or {}
        who = str(p.get("streamer") or "the stream")
        return {
            "live": _line(f"Getting ready to watch {who}."),
            "vod": _line(f"Opening {who}'s broadcast."),
            "tests": _line("Checking that every part of the bot still works."),
            "render": _line("Making this clip's Short again."),
        }.get(str(event.get("run_kind")))
    if kind == "recording":
        where = PLATFORMS.get(str(event.get("platform")), "the platform")
        return _line(f"Joined {who}'s stream on {where}. Recording has started.", "good")
    if kind == "baseline":
        what = f" of {event['category']}" if event.get("category") else ""
        more = " The rest is being read in the background." if event.get("more") else ""
        return _line(
            f"Caught up on the chat: {_n(event.get('messages'))} messages from {_hours(event.get('hours'))}"
            f"{what}.{more}",
            "good",
            "baseline",
        )
    if kind == "watching":
        rate = f", about {_n(event['per_minute'])} a minute" if event.get("per_minute") else ""
        return _line(
            f"Watching the chat for a moment worth clipping: {_n(event.get('messages'))} messages so far"
            f"{rate}.",
            "info",
            "watching",
        )
    if kind == "chat":
        return _line(f"Read the whole chat: {_n(event.get('messages'))} messages.", "good", "step:chat")
    if kind == "candidates":
        count = int(event.get("count") or 0)
        if not count:
            return _line("Nothing in this broadcast stood out.", "warn")
        what = f" while they played {event['category']}" if event.get("category") else ""
        return _line(f"{count} {'moment' if count == 1 else 'moments'} stood out{what}.", "good")
    if kind == "moment":
        secs = f"{float(event.get('seconds') or 0):g}"
        if int(event.get("chatters") or 0) > 0:
            return _line(
                f"Chat lit up: {_n(event['chatters'])} people reacting at once"
                f" (score {event.get('chat_score')}). Keeping {secs} seconds of it.",
                "good",
            )
        return _line(f"Making a clip of {secs} seconds.", "good")
    if kind == "forced":
        return _line(f"You asked for a clip of the last {float(event.get('seconds') or 0):g} seconds.")
    if kind == "step":
        step, state = str(event.get("step")), event.get("state")
        name, doing, done = STEPS.get(step, (step, step, step))
        key = f"step:{step}"
        if state == "start":
            return _line(doing + "…", "info", key)
        if state == "failed":
            why = problem(event.get("error"))
            return _line(f"{doing} did not work. {why['text']} {why['fix']}".strip(), "bad", key)
        if float(event.get("seconds") or 0) < 0.1 and step in SKIPPED:
            # over at once: there was nothing for it to do
            return (
                _line(SKIPPED[step], "info", key) if SKIPPED[step] else {"text": "", "key": key, "drop": True}
            )
        return _line(f"{done}{_took(event.get('seconds'))}.", "info", key)
    if kind == "judged":
        title = str(event.get("title") or "").strip()
        named = f": “{title}”" if title else ""
        if event.get("source") == "claude":
            return _line(
                f"The judge gave it {event.get('score')} out of 10 and wrote the title{named}.", "good"
            )
        return _line(f"No judge on this run, so the built-in rules wrote the title{named}.")
    if kind == "staged":
        if event.get("again"):
            return _line("The Short was made again.", "good")
        title = str(event.get("title") or "").strip()
        named = f": “{title}”" if title else ""
        return _line(f"Clip ready{named}. It now waits for a person to approve it.", "good")
    if kind == "rejected":
        return _line("Not kept. " + rejection(event.get("reason")), "warn")
    if kind == "failed":
        return _line("This clip could not be made. " + problem(event.get("reason"))["text"], "bad")
    if kind == "tests_done":
        bad = int(event.get("failed") or 0) + int(event.get("error") or 0)
        took = _took(event.get("seconds")).strip()
        if bad:
            return _line(
                f"{_n(event.get('passed'))} checks passed and {bad} did not {took}".strip() + ".", "bad"
            )
        return _line(f"All {_n(event.get('passed'))} checks passed {took}".strip() + ".", "good")
    if kind == "stopping":
        return _line("Stopping. What is in progress is finished first.")
    if kind == "end":
        state = event.get("state")
        if state == "failed":
            return _line("It ended early because of a problem.", "bad")
        return _line("Stopped." if state == "stopped" else "Finished.", "good")
    if kind == "log":
        return explain(str(event.get("line") or ""))
    return None


# log lines a person should hear about -> (sentence, tone). \\1.. are the groups of the pattern.
_LINES: tuple[tuple[str, str, str], ...] = (
    (
        r"faster-whisper not installed",
        "There is no speech model on this machine, so this clip gets no captions.",
        "warn",
    ),
    (
        r"no face detector",
        "There is no face detector on this machine, so the layout set for the streamer is used.",
        "warn",
    ),
    (
        r"render with the Remotion overlay failed",
        "The animated captions could not be drawn, so the built-in style was used.",
        "warn",
    ),
    (
        r"caption colours: .*using the fixed colours",
        "The clip's own colours could not be read, so the template's colours are used.",
        "info",
    ),
    (r"buffer wait timed out", "The recording was a little behind, so the clip uses what was there.", "info"),
    (
        r": ([\d.]+) s of ([\d.]+) s rendered, the recording ends there",
        r"The recording ended early: the clip is \1 seconds long instead of \2.",
        "info",
    ),
    (r"recorder exited .*restarting", "The recording stopped for a moment and was started again.", "warn"),
    (r"recording caught up with live", "The recording is back in step with the stream.", "good"),
    (r"disk free again", "There is room on the disk again, so recording carries on.", "good"),
    (
        r"kick \S+: no realtime connection",
        "Kick's live chat could not be opened, so the chat is read a few seconds late.",
        "info",
    ),
    (
        r"kick \S+: realtime connection (lost|closed)",
        "The connection to Kick's chat dropped. Reconnecting.",
        "info",
    ),
    (
        r"(channel|broadcast) lookup failed|history poll failed",
        "The platform did not answer. Trying again.",
        "info",
    ),
    (
        r"loading chat history failed",
        "The earlier chat could not be read. The bot goes on with the live chat and needs a few minutes to"
        " learn what is normal here.",
        "warn",
    ),
    (
        r"reading the older chat failed",
        "Part of the earlier chat could not be read. What was read is used.",
        "info",
    ),
    (r": now streaming (.+?) \(was ", r"The stream switched to \1.", "info"),
    (r"viewer clips confirm the spike", "Viewers clipped this moment too.", "good"),
    (r"skip weak moment", "A small spike in chat, not strong enough for a clip.", "info"),
    (
        r"lane crashed; restarting",
        "Something went wrong, so the bot restarted itself and carries on.",
        "warn",
    ),
    (
        r": skipped, speaks '?(\w+)'? but the channel is",
        "This streamer is skipped: the channel posts in one language and they speak another.",
        "warn",
    ),
    (
        r"reading the chat of ([\d.]+) h \(Kick",
        r"Reading \1 hours of chat. Kick hands it out slowly, so this takes a few minutes.",
        "info",
    ),
    (r"^no candidates$", "Nothing in this broadcast stood out.", "warn"),
    (r"^stopped: the clip keeps the video it had", "Stopped. The clip keeps the video it had.", "info"),
    (
        r"frames/loudness for the judge failed",
        "The judge got the words only: its pictures could not be made.",
        "info",
    ),
    (
        r"loudness read failed",
        "How loud the moment was could not be measured. The chat alone decides.",
        "info",
    ),
)
_LINES_C = tuple((re.compile(p, re.I), text, tone) for p, text, tone in _LINES)
_FAILED_BECAUSE = re.compile(r"^(?:judge|screen) failed \((.*)\)", re.I)
_NOT_RENDERED = re.compile(r"^(?:not rendered|could not start|failed #\d+): (.*)", re.I)
_RAISED = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Interrupt|Exit)\b(?::|$)")


def explain(line: str) -> dict[str, Any] | None:
    """A log line as a line of the story, when it is one a person should hear about."""
    text = line.strip()
    if not text:
        return None
    for rx, said, tone in _LINES_C:
        m = rx.search(text)
        if m:
            return _line(m.expand(said) if "\\" in said else said, tone)
    m = _FAILED_BECAUSE.match(text)
    if m:
        why = problem(m.group(1))
        asked = (
            "The judge could not be asked"
            if text.lower().startswith("judge")
            else "The quick look could not run"
        )
        return _line(f"{asked}. {why['text'] if why['known'] else 'The built-in rules take over.'}", "warn")
    m = _NOT_RENDERED.match(text)
    if m:
        why = problem(m.group(1))
        return _line(why["text"] + (" " + why["fix"] if why["fix"] else ""), "bad")
    if _RAISED.match(text) and not text.startswith("KeyboardInterrupt"):  # the last line of a traceback
        why = problem(text)
        return _line(why["text"] + (" " + why["fix"] if why["fix"] else ""), "bad")
    return None


def dress(event: dict[str, Any]) -> dict[str, Any]:
    """The event as the page gets it: with its line of the story ("say") and, when it reports a failure,
    the failure in plain words ("problem"). The event itself is left as it was."""
    out = dict(event)
    try:
        said = say(event)
        if said:
            out["say"] = said
        if event.get("kind") == "step" and event.get("state") == "failed":
            out["problem"] = problem(event.get("error"))
        elif event.get("kind") == "failed":
            out["problem"] = problem(event.get("reason"))
        elif event.get("kind") == "rejected":
            out["problem"] = {"text": rejection(event.get("reason")), "fix": "", "known": True}
    except Exception:  # noqa: BLE001 - a line that cannot be worded must never cost the page the event
        out.pop("say", None)
    return out
