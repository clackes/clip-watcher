from clipwatcher.chat.convert import read_jsonl, tdl_to_messages, write_jsonl
from clipwatcher.detect.baseline import x_normal_top
from clipwatcher.detect.detector import confirm, detect
from tests.fixtures import synth


def _near(peaks, moment, tol=15):
    return any(abs(p - moment) <= tol for p in peaks)


def test_detector_finds_planted_spikes_and_rejects_raid_giveaway():
    msgs = synth.make_chat()
    cands = detect(msgs, streamer="examplestreamer", top=8, before_s=20, after_s=10, gap_s=60)
    peaks = [c.peak_s for c in cands]
    for m in synth.SPIKES:
        assert _near(peaks, m), (m, peaks)
    # raid and giveaway bursts (chat reacts to them at ~+1..15 s; peak is delayed back by 8 s)
    for bad in (synth.RAID_AT, synth.GIVEAWAY_AT):
        assert not any(bad - 20 <= p <= bad + 90 for p in peaks), (bad, peaks)
    assert len(cands) == 3
    for c in cands:
        assert c.start_s >= 0 and c.end_s > c.start_s and c.x_normal > 1


def test_baseline_would_pick_raid_and_giveaway():
    msgs = synth.make_chat()
    base = x_normal_top(msgs, top=6, before=20, after=10, gap=60)
    peaks = [c.peak_s for c in base]
    assert any(synth.RAID_AT <= p <= synth.RAID_AT + 20 for p in peaks)
    assert any(synth.GIVEAWAY_AT <= p <= synth.GIVEAWAY_AT + 15 for p in peaks)


def test_confirm_hooks():
    c = detect(synth.make_chat(), streamer="x", top=1)[0]
    c2 = confirm(c, viewer_clip_times=[c.peak_s + 5], loud_z=2.0)
    assert c2.final_score > c.chat_score and c.final_score == c.chat_score


def test_empty_and_tiny():
    assert detect([], streamer="x") == []


def test_convert_roundtrip(tmp_path):
    msgs = synth.make_chat()
    p = synth.to_tdl_json(msgs, tmp_path / "chat.json")
    conv = tdl_to_messages(p)
    assert len(conv) == len(msgs)
    assert any(m.is_notice and m.notice_type == "raid" and m.viewers == 300 for m in conv)
    assert any(m.is_bot and m.user == "nightbot" for m in conv)
    out = write_jsonl(conv, tmp_path / "chat.jsonl")
    assert read_jsonl(out) == conv
