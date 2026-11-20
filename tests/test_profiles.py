"""Streamer profiles for the companion app: asked once, kept on disk, served from there."""

import http.client
import json
import threading

import pytest

from clipwatcher.ui.profiles import Profiles, from_kick, from_twitch, image_type
from clipwatcher.ui.runs import RunManager
from clipwatcher.ui.server import App, make_server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30
CDN = "https://static-cdn.jtvnw.net"
PARTNER = {"setID": "partner", "version": "1", "title": "Verified", "imageURL": f"{CDN}/badges/v1/partner/2"}
CLIX = {
    "login": "clix",
    "displayName": "Clix",
    "profileImageURL": f"{CDN}/jtv_user_pictures/clix-150x150.png",
    "primaryColorHex": "FA2929",
    "roles": {"isPartner": True, "isAffiliate": False},
    "followers": {"totalCount": 8732363},
    "displayBadges": [
        {"setID": "twitchcon", "version": "1", "title": "TwitchCon", "imageURL": f"{CDN}/badges/v1/tc/2"}
    ],
}
RATED = {
    "verified": True,
    "is_affiliate": False,
    "followers_count": 31403,
    "user": {"username": "RatedEpicz", "profile_pic": "https://files.kick.com/images/user/1/pic.webp"},
    "subscriber_badges": [
        {"months": 1, "badge_image": {"src": "https://files.kick.com/channel_subscriber_badges/1/a.png"}},
        {"months": 12, "badge_image": {"src": "https://files.kick.com/channel_subscriber_badges/12/a.png"}},
    ],
}


class Net:
    """Stands in for Twitch, Kick and their image hosts, and counts what was asked."""

    def __init__(self):
        self.asked, self.down, self.fail = [], [], False

    def twitch(self, login):
        self.asked.append(("twitch", login))
        if self.fail:
            raise OSError("offline")
        return CLIX

    def kick(self, slug):
        self.asked.append(("kick", slug))
        if self.fail:
            raise OSError("offline")
        return RATED

    def download(self, url):
        self.down.append(url)
        return WEBP if url.endswith(".webp") else PNG


@pytest.fixture
def kept(tmp_path):
    net, clock = Net(), [1000.0]
    profiles = Profiles(
        tmp_path / "profiles",
        twitch=net.twitch,
        kick=net.kick,
        partner_badge=lambda: PARTNER,
        download=net.download,
        now=lambda: clock[0],
    )
    return profiles, net, clock


def test_twitch_and_kick_answers_become_profiles():
    t = from_twitch("clix", CLIX, PARTNER)
    assert (t["name"], t["verified"], t["followers"], t["colour"]) == ("Clix", True, 8732363, "#FA2929")
    assert [b["id"] for b in t["badges"]] == [
        "partner",
        "twitchcon",
    ]  # a partner's Verified badge comes first
    assert from_twitch("small", {**CLIX, "roles": {}, "displayBadges": []}, None)["badges"] == []
    k = from_kick("ratedepicz", RATED)
    assert (k["name"], k["verified"], k["followers"]) == ("RatedEpicz", True, 31403)
    assert [(b["id"], b["url"] is None) for b in k["badges"]] == [
        ("verified", True),
        ("sub-12", False),
        ("sub-1", False),
    ]
    assert from_kick("nobody", {})["name"] == "nobody" and from_kick("nobody", {})["badges"] == []


def test_a_profile_is_asked_once_and_then_read_from_disk(kept):
    profiles, net, clock = kept
    first = profiles.get("twitch", "Clix")
    assert first["name"] == "Clix" and first["verified"] and first["known"] and first["colour"] == "#FA2929"
    assert first["avatar"].startswith("/profile/image/") and [b["title"] for b in first["badges"]] == [
        "Verified",
        "TwitchCon",
    ]
    assert net.asked == [("twitch", "clix")] and len(net.down) == 3  # the picture and two badges
    again = Profiles(
        profiles.root, twitch=net.twitch, kick=net.kick, download=net.download, now=lambda: clock[0]
    )
    assert again.get("twitch", "clix") == first and net.asked == [("twitch", "clix")] and len(net.down) == 3
    clock[0] += 25 * 3600  # a day later it is asked again; pictures at the same address are not fetched twice
    assert profiles.get("twitch", "clix") == first and len(net.asked) == 2 and len(net.down) == 3
    path, ctype = profiles.image(first["avatar"].rsplit("/", 1)[1])
    assert path.read_bytes() == PNG and ctype == "image/png"
    assert profiles.image("../../etc/passwd") is None and profiles.image("0" * 20) is None


def test_a_lookup_that_fails_keeps_what_was_known(kept):
    profiles, net, clock = kept
    net.fail = True
    bare = profiles.get("kick", "ratedepicz")
    assert bare == {**bare, "name": "ratedepicz", "avatar": None, "badges": [], "known": False}
    profiles.get("kick", "ratedepicz")
    assert len(net.asked) == 1  # not asked again at once
    net.fail, clock[0] = False, clock[0] + 700
    full = profiles.get("kick", "ratedepicz")
    assert full["name"] == "RatedEpicz" and full["verified"] and full["avatar"]
    assert [(b["id"], bool(b["image"])) for b in full["badges"]] == [
        ("verified", False),
        ("sub-12", True),
        ("sub-1", True),
    ]
    assert profiles.image(full["avatar"].rsplit("/", 1)[1])[1] == "image/webp"
    net.fail, clock[0] = True, clock[0] + 25 * 3600
    assert profiles.get("kick", "ratedepicz") == full  # stale, but better than nothing


def test_only_pictures_from_the_platforms_hosts_are_kept(tmp_path):
    seen = []

    def download(url):
        seen.append(url)
        return b"<svg onload=alert(1)>" if "svg" in url else PNG

    p = Profiles(tmp_path, download=download)
    assert p._keep_image("http://static-cdn.jtvnw.net/a.png") is None  # not https
    assert (
        p._keep_image("https://evil.example/a.png") is None
        and p._keep_image("https://jtvnw.net.evil.example/a.png") is None
    )
    assert p._keep_image("https://files.kick.com.evil.example/a.png") is None and seen == []
    assert p._keep_image("https://files.kick.com/a.svg") is None  # fetched, but not a picture we serve
    assert p._keep_image("https://files.kick.com/a.png") and len(seen) == 2
    assert (
        image_type(PNG) == "image/png" and image_type(WEBP) == "image/webp" and image_type(b"<html>") is None
    )
    for bad in (("youtube", "clix"), ("twitch", "a b"), ("twitch", "../x")):
        with pytest.raises(ValueError):
            p.get(*bad)


def test_the_server_hands_out_profiles_and_their_pictures(kept, tmp_path):
    profiles, net, _ = kept
    app = App(RunManager(tmp_path / "runs"), profiles=profiles)
    server, app = make_server("127.0.0.1", 0, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()

        status, _, body = get("/api/profiles?who=twitch:clix,kick:ratedepicz")
        rows = json.loads(body)
        assert status == 200 and [(r["platform"], r["name"]) for r in rows] == [
            ("twitch", "Clix"),
            ("kick", "RatedEpicz"),
        ]
        status, headers, picture = get(rows[1]["avatar"])
        assert status == 200 and picture == WEBP and headers["Content-Type"] == "image/webp"
        assert "immutable" in headers["Cache-Control"] and headers["X-Content-Type-Options"] == "nosniff"
        assert get("/profile/image/" + "f" * 20)[0] == 404 and get("/profile/image/..%2F..%2Fx")[0] == 404
        assert get("/api/profiles?who=youtube:clix")[0] == 400 and json.loads(get("/api/profiles")[2]) == []
    finally:
        server.shutdown()
        server.server_close()
