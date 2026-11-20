"""The run's story in plain words (ui/friendly.py): what a person reads instead of the lane's log."""

from __future__ import annotations

import re

from clipwatcher import events as lane_events
from clipwatcher.ui import data, friendly

TECHNICAL = re.compile(r"Traceback|Errno|\.py\b|/Users/|clipwatcher\.|Exception|Error:|\bexit code\b", re.I)


def test_every_step_a_lane_reports_has_a_name_and_both_sentences():
    assert set(data.STEP_ORDER) <= set(friendly.STEPS)
    assert data.STEP_NAMES is friendly.STEP_NAMES
    for step, (name, doing, done) in friendly.STEPS.items():
        assert name and doing and done, step
        started = friendly.say({"kind": "step", "step": step, "state": "start"})
        ended = friendly.say({"kind": "step", "step": step, "state": "done", "seconds": 12.4})
        assert started["key"] == ended["key"] == f"step:{step}"  # one line that changes
        assert started["text"].endswith("…") and ended["text"].endswith(".")


def test_a_live_run_reads_as_a_story():
    said = [
        friendly.say(e)["text"]
        for e in (
            {"kind": "start", "run_kind": "live", "params": {"streamer": "clix"}},
            {"kind": "recording", "streamer": "clix", "platform": "twitch", "ring": "/work/ring/clix/1"},
            {"kind": "baseline", "streamer": "clix", "messages": 38756, "hours": 12.0, "category": "GTA VI"},
            {"kind": "watching", "streamer": "clix", "messages": 1204, "per_minute": 85},
            {"kind": "moment", "streamer": "clix", "chat_score": 9.9, "chatters": 48, "seconds": 59.0},
            {"kind": "step", "step": "render_short", "state": "done", "seconds": 125.1},
            {"kind": "judged", "source": "heuristic", "score": 0, "title": "Chat saw it first"},
            {"kind": "staged", "title": "Chat saw it first"},
            {"kind": "end", "state": "done"},
        )
    ]
    assert said[1] == "Joined clix's stream on Twitch. Recording has started."
    assert "38,756 messages from 12 hours of GTA VI" in said[2]
    short = {"kind": "baseline", "streamer": "clix", "messages": 310, "hours": 0.42}
    assert "310 messages from the last 25 minutes." in friendly.say(short)["text"]
    assert "from 6.7 hours" in friendly.say({**short, "hours": 6.71})["text"]
    assert "48 people reacting at once" in said[4] and "59 seconds" in said[4]
    assert said[5] == "Made the vertical Short (2 min 5 s)."
    assert "built-in rules wrote the title" in said[6] and "Chat saw it first" in said[6]
    assert said[7].startswith("Clip ready") and "approve" in said[7]
    assert not any(TECHNICAL.search(s) for s in said), said


def test_errors_are_said_in_plain_words_with_what_to_do():
    cases = {
        "FileNotFoundError: [Errno 2] No such file or directory: 'bin/TwitchDownloaderCLI'": "Twitch",
        "OSError: [Errno 28] No space left on device": "disk is full",
        "CalledProcessError: Command '['/opt/homebrew/bin/ffmpeg', '-y']' returned exit status 1": "video",
        "URLError: <urlopen error [Errno 8] nodename nor servname provided>": "internet connection",
        "HTTPError: HTTP Error 429: Too Many Requests": "slow down",
        "anthropic.AuthenticationError: Error code: 401 - invalid x-api-key": "key was not accepted",
        "RuntimeError: clix-live-1 is APPROVED, not READY": "already decided",
        "Could not resolve authentication method. Expected one of api_key": "no key for the judge",
    }
    for raw, part in cases.items():
        p = friendly.problem(raw)
        assert p["known"] and part in p["text"], raw
        assert not TECHNICAL.search(p["text"] + p["fix"]), p
    unknown = friendly.problem("ZeroDivisionError: division by zero")
    assert (
        not unknown["known"] and unknown["text"] == friendly.UNKNOWN and "technical details" in unknown["fix"]
    )


def test_a_failed_step_keeps_its_error_and_gains_the_sentence():
    e = {
        "kind": "step",
        "step": "chat",
        "state": "failed",
        "error": "FileNotFoundError: bin/TwitchDownloaderCLI",
    }
    out = friendly.dress(e)
    assert out["error"] == e["error"] and "problem" not in e  # the event itself is left as it was
    assert out["say"]["tone"] == "bad" and "did not work" in out["say"]["text"]
    assert out["problem"]["known"] and "Twitch" in out["problem"]["text"]


def test_a_step_that_was_over_at_once_is_not_said_to_have_done_its_work():
    """No speech model, no judge, no detector: the step ends in an instant and the story says so."""
    for step, text in friendly.SKIPPED.items():
        got = friendly.say({"kind": "step", "step": step, "state": "done", "seconds": 0.0})
        assert got["text"] == text and got["key"] == f"step:{step}"
        assert bool(got.get("drop")) == (not text)  # nothing to say: the line that announced it is taken back
    worked = friendly.say({"kind": "step", "step": "transcribe", "state": "done", "seconds": 8.2})
    assert worked["text"] == "Wrote down what was said (8 s)."


def test_a_rejection_says_why_in_words():
    assert friendly.rejection("flags: sp,mu") == (
        "The judge passed on it: it gives away a spoiler and there is music that could be claimed."
    )
    assert friendly.rejection("Nothing happens.") == "The judge passed on it: Nothing happens."
    assert friendly.rejection("before recording start") == "The moment happened before the recording began."
    assert friendly.dress({"kind": "rejected", "reason": "flags: gb"})["problem"]["text"].endswith(
        "gambling."
    )


def test_log_lines_a_person_should_hear_about_are_explained_and_the_rest_stay_technical():
    heard = {
        "faster-whisper not installed; live transcript skipped": "no speech model",
        "judge failed (No module named 'anthropic'); using heuristic": "judge could not be asked",
        "WARNING clipwatcher.render.template: short_9x16.mp4: 41.2 s of 59.0 s rendered,"
        " the recording ends there": "41.2 seconds long instead of 59.0",
        "reading the chat of 2.3 h (Kick allows about 35 messages a second) ...": "2.3 hours of chat",
        "FileNotFoundError: [Errno 2] No such file or directory: 'bin/TwitchDownloaderCLI'": "not installed",
        "2026-11-20 04:17:15,686 WARNING clipwatcher.chat.kick: kick n3on: realtime connection lost"
        " (ConnectionClosed: x)": "Reconnecting",
    }
    for line, part in heard.items():
        got = friendly.explain(line)
        assert got and part.lower() in got["text"].lower(), line
        assert not TECHNICAL.search(got["text"]), got
    for line in (
        "2026-11-20 04:16:01,680 INFO clipwatcher.live: clix: recording to /Users/x/work/ring/clix/20261120",
        '  File "/Users/x/src/clipwatcher/vod.py", line 61, in run_cmd',
        "Traceback (most recent call last):",
        "    subprocess.run(cmd, check=True)",
        " 1 1:35:49   15.83    2.4   26",
        "",
    ):
        assert friendly.explain(line) is None, line


def test_an_event_nobody_worded_goes_through_untouched():
    odd = {"kind": "something_new", "value": 3}
    assert friendly.dress(odd) == odd
    assert friendly.dress({"kind": "step"})["say"]["text"]  # half an event is still worded, not an error


def test_the_events_the_lanes_emit_are_all_worded():
    """Every kind of event a lane writes gets a line (a kind added later must be worded here too)."""
    assert lane_events.emit and lane_events.step
    kinds = "recording baseline watching chat candidates moment forced judged staged rejected failed stopping"
    for kind in (*kinds.split(), "end", "tests_done"):
        assert friendly.say({"kind": kind, "state": "done"}), kind
