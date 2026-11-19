"""D16: scripts/learn_keywords.py finds the words that mark a moment, per category and per streamer."""

import importlib.util
import json
import random
from pathlib import Path

import pytest
import yaml

from clipwatcher import config
from clipwatcher.chat.convert import write_jsonl
from clipwatcher.models import ChatMessage

_SPEC = importlib.util.spec_from_file_location(
    "learn_keywords", Path(__file__).parents[1] / "scripts" / "learn_keywords.py"
)
lk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lk)

T0 = 1_790_000_000  # start of every made-up broadcast, epoch seconds
TALK = "the is you it he and this that of for what was with just car road mission police nice".split()


def test_chat_words_are_read_as_a_keyword_list_would_hold_them():
    zero_width = chr(0x34F)  # what chat clients append to get past the duplicate-message filter
    said = f'LMAOOO{zero_width} what?! ??? @bob !drop https://x.y "clip it!!"'
    assert lk.raw_tokens(said) == ["lmaooo", "what", "???", "clip", "it"]
    known = {"lmao": 9, "way": 5, "wayy": 1, "kill": 4, "kil": 0}
    assert [lk.fold(w, known) for w in ("lmaoooo", "wayyy", "killll", "wwww", "???")] == [
        "lmao", "way", "kill", "w", "?",
    ]  # fmt: skip
    assert lk.features("NO WAYYY bro", known) == {"no", "way", "bro", "no way", "way bro"}
    assert lk.features("one two three four five six seven", known) == set(
        "one two three four five six seven".split()
    )


def test_clips_of_the_same_moment_are_one_zone():
    seg = {"vod": "9", "created_ms": T0 * 1000, "start_s": 0, "end_s": 7200}
    clips = [
        {"video": {"id": "9"}, "videoOffsetSeconds": 600, "durationSeconds": 30},
        {"video": {"id": "9"}, "videoOffsetSeconds": 615, "durationSeconds": 28},  # the same moment
        {"video": {"id": "9"}, "videoOffsetSeconds": 3000, "durationSeconds": 20},
        {"video": {"id": "8"}, "videoOffsetSeconds": 700, "durationSeconds": 30},  # another broadcast
        {"video": None, "videoOffsetSeconds": None, "durationSeconds": 30},  # its VOD is gone
        {"video": {"id": "9"}, "videoOffsetSeconds": 7190, "durationSeconds": 30},  # runs past the stretch
    ]
    assert lk.clip_zones(seg, clips) == [(T0 + 600, T0 + 663), (T0 + 3000, T0 + 3040)]


def _channel(root: Path, login: str, rng: random.Random, emote: str) -> Path:
    """Two hours of made-up chat with twelve clipped moments. In every moment chat types WASTED, in most
    the channel's own emote, and in one of them a word about what happened there."""
    d = root / login
    d.mkdir(parents=True)
    msgs, clips = [], []
    for t in range(7200):
        text = " ".join(rng.choice(TALK) for _ in range(rng.randint(2, 6)))
        msgs.append(ChatMessage((T0 + t) * 1000 + rng.randrange(1000), f"v{rng.randrange(400)}", text))
    for n in range(12):
        at = 500 + n * 520
        clips.append(
            {"video": {"id": "1"}, "videoOffsetSeconds": at, "durationSeconds": 30, "slug": f"{login}{n}"}
        )
        for i in range(40):
            said = rng.choice(["WASTED", "wasted lol", "WASTEDDD"])
            if n % 4 != 3 and i % 2:
                said = emote
            if n == 5 and i % 3 == 0:
                said = "helicopter"  # what this one moment was about
            msgs.append(ChatMessage(int((T0 + at + 18 + rng.uniform(0, 12)) * 1000), f"fan{i}", said))
    write_jsonl(sorted(msgs, key=lambda m: m.ts_ms), d / "1_0_7200.jsonl")
    seg = {"vod": "1", "created_ms": T0 * 1000, "start_s": 0, "end_s": 7200, "category": True}
    (d / "segments.json").write_text(json.dumps([seg]))
    (d / "clips.json").write_text(json.dumps(clips))
    return d


@pytest.fixture
def stats(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "streamers", lambda: {})
    rng = random.Random(7)
    return [lk.channel_stats(_channel(tmp_path, f"chan{i}", rng, f"chan{i}Laugh")) for i in range(6)]


def test_category_words_come_back_in_every_channel_and_streamer_words_in_one(stats):
    a = lk.parser().parse_args(
        ["learn", "--category", "Grand Theft Auto VI", "--streamer-moments-channel", "6"]
    )
    assert all(s["clipped"] == 12 and s["moments"] >= 12 and s["english"] > 0.3 for s in stats)
    category = lk.category_words(stats, a)
    assert "wasted" in category and category["wasted"]["channels"] == 6 and category["wasted"]["weight"] >= 2
    assert not set(category) & {"helicopter", "the", "car", "chan0laugh"}  # one moment, talk, one channel
    own = lk.streamer_words(stats[0], category, set(), a)
    assert list(own) == ["chan0laugh"] and 2 <= own["chan0laugh"]["weight"] <= 2.5
    # the half kept for testing: learned without the moments around every second viewer clip
    half = lk.streamer_words(stats[0], category, set(), a, part="even")
    assert list(half) == ["chan0laugh"] and half["chan0laugh"]["moments"] < own["chan0laugh"]["moments"]
    assert lk.streamer_words(stats[0], category, {"chan0laugh"}, a) == {}  # plain talk in every other chat
    assert {"the", "car", "police"} <= lk.plain_words(stats, a) and "wasted" not in lk.plain_words(stats, a)
    # a channel in another language teaches the category nothing
    foreign = [{**s, "english": 0.001} for s in stats]
    assert lk.category_words(foreign, a) == {}


def test_a_word_another_word_already_matches_is_left_out():
    words = {
        "gyat": {"weight": 2.0, "moments": 40},
        "gyatt": {"weight": 2.0, "moments": 12},  # gyat typed with a held key
        "crazy": {"weight": 2.0, "moments": 30},
        "she crazy": {"weight": 2.0, "moments": 9},  # no stronger than crazy alone
        "no way": {"weight": 2.5, "moments": 20},
        "way": {"weight": 1.5, "moments": 25},
    }
    assert set(lk.drop_covered(words)) == {"gyat", "crazy", "no way", "way"}
    assert set(lk.drop_covered(words, known={"crazy": {"weight": 2.0}})) == {"gyat", "no way", "way"}


def test_learned_words_are_merged_into_the_keyword_file(stats, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "keywords.yaml").write_text(
        yaml.safe_dump(
            {
                "categories": {"fortnite": {"keywords": {"cracked": 2.0}}},
                "streamers": {"x": {"keywords": {"a": 1}}},
            }
        )
    )
    words = {"wasted": {"weight": 2.5, "moments": 70}, "busted": {"weight": 1.5, "moments": 20}}
    streamers = {
        "chan0": {"chan0laugh": {"weight": 2.0, "moments": 9}},
        "other": {"zzz": {"weight": 2.0, "moments": 5}},
    }
    lk.write_keywords("Grand Theft Auto VI", words, streamers, stats, only={"chan0"})
    got = yaml.safe_load((tmp_path / "keywords.yaml").read_text())
    gta = got["categories"]["grand-theft-auto-vi"]
    assert gta["keywords"] == {"wasted": 2.5, "busted": 1.5} and gta["match"] == ["grand theft auto vi"]
    assert "6 channels" in gta["learned"] and got["categories"]["fortnite"] == {"keywords": {"cracked": 2.0}}
    assert got["streamers"] == {"x": {"keywords": {"a": 1}}, "chan0": {"keywords": {"chan0laugh": 2.0}}}
    # and the detector reads it back: the category's words on a GTA stream, the streamer's on that channel
    monkeypatch.setattr(config, "global_config", lambda: {"detector": {"keywords": {"KEKW": 2}}})
    assert config.keywords_for("chan0", "Grand Theft Auto VI (GTA)") == {
        "KEKW": 2, "wasted": 2.5, "busted": 1.5, "chan0laugh": 2.0,
    }  # fmt: skip
    assert config.keywords_for("chan0", "Just Chatting") == {"KEKW": 2, "chan0laugh": 2.0}
