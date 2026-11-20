"""The YouTube accounts the browser publisher posts from, and the sign-in that fills them. The browser is a
stand-in: these tests never start Chrome and never go to the network."""

import asyncio
import json

import pytest

from clipwatcher.publish import youtube_accounts, youtube_browser, youtube_login
from clipwatcher.publish.youtube_accounts import Accounts

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
UC1, UC2 = "UC" + "a" * 22, "UC" + "b" * 22
PAGE = (
    '<html><head><meta property="og:title" content="Clip &amp; Vault">'
    '<meta property="og:image" content="https://yt3.googleusercontent.com/abc=s900-c-k-c0x00ffffff-no-rj">'
    '<link rel="canonical" href="https://www.youtube.com/channel/' + UC1 + '"></head><body><script>'
    'var ytInitialData = {"other":{"canonicalBaseUrl":"/@someoneelse"},"metadata":{"channelMetadataRenderer":'
    '{"vanityChannelUrl":"http://www.youtube.com/@clipvault"}},"x":{"content":"12.4K subscribers"}};'
    "</script></body></html>"
)


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPWATCHER_ACCOUNTS", str(tmp_path / "home" / "youtube"))
    monkeypatch.delenv("YT_BROWSER_PROFILE", raising=False)
    return Accounts(download=lambda url: PNG)


# -- the registry --------------------------------------------------------------------------------------------
def test_an_account_is_listed_once_a_channel_was_read(accounts):
    rec = accounts.create()
    assert accounts.list() == [] and accounts.posting() is None  # nobody signed in yet
    accounts.found(
        rec["id"],
        {
            "channel_id": UC1,
            "name": "Clip & Vault",
            "handle": "@clipvault",
            "subscribers": 12400,
            "picture_url": "https://yt3.googleusercontent.com/abc=s240",
        },
    )
    (row,) = accounts.list()
    assert row["posting"] and row["state"] == "ready" and row["checked"] >= row["added"]
    shown = Accounts.public(row)
    assert (
        shown["name"] == "Clip & Vault" and shown["handle"] == "@clipvault" and shown["subscribers"] == 12400
    )
    assert (
        shown["picture"].startswith("/connect/image/")
        and "profile" not in shown
        and "picture_url" not in shown
    )
    path, ctype = accounts.image(shown["picture"].rsplit("/", 1)[1])
    assert ctype == "image/png" and path.read_bytes() == PNG
    assert accounts.profile_path(rec["id"]) == accounts.root / rec["id"] / "profile"


def test_the_account_that_posts_is_the_one_chosen_else_the_first(accounts):
    ids = []
    for n, cid in enumerate((UC1, UC2)):
        rec = accounts.create()
        accounts.update(rec["id"], added=100.0 + n)
        accounts.found(rec["id"], {"channel_id": cid, "name": f"Channel {n}"})
        ids.append(rec["id"])
    assert [r["posting"] for r in accounts.list()] == [True, False]
    accounts.set_posting(ids[1])
    assert accounts.posting()["name"] == "Channel 1"
    assert youtube_accounts.posting_profile() == accounts.root / ids[1] / "profile"
    with pytest.raises(ValueError):
        accounts.set_posting("0123456789ab")
    accounts.remove(ids[1])  # the one that posted is gone: the other one posts
    assert [(r["name"], r["posting"]) for r in accounts.list()] == [("Channel 0", True)]
    later = accounts.create()
    accounts.found(later["id"], {"channel_id": "UC" + "c" * 22, "name": "Later"})
    accounts.update(
        ids[0], state=youtube_accounts.SIGN_IN
    )  # nobody chose: a signed-out account does not post
    assert accounts.posting()["name"] == "Later"
    accounts.set_posting(ids[0])  # a choice is kept, and the page says it needs signing in
    assert accounts.posting()["name"] == "Channel 0"
    assert not (accounts.root / ids[1]).exists()
    assert accounts.same_channel(ids[1], UC1)["id"] == ids[0] and accounts.same_channel(ids[0], UC1) is None


def test_pictures_come_from_googles_hosts_only_and_are_checked_by_their_first_bytes(tmp_path):
    asked = []

    def download(url):
        asked.append(url)
        return {"png": PNG, "html": b"<html>", "big": PNG + b"0" * youtube_accounts.MAX_IMAGE}[
            url.rsplit("/", 1)[1]
        ]

    acc = Accounts(tmp_path, download=download)
    assert acc.keep_image("http://yt3.ggpht.com/png") is None  # not https
    assert (
        acc.keep_image("https://evil.example/png") is None
        and acc.keep_image("https://ggpht.com.evil.io/png") is None
    )
    assert asked == []
    assert (
        acc.keep_image("https://yt3.ggpht.com/html") is None
        and acc.keep_image("https://yt3.ggpht.com/big") is None
    )
    kept = acc.keep_image("https://yt3.ggpht.com/png")
    assert acc.image(kept)[1] == "image/png" and acc.keep_image("https://yt3.ggpht.com/png") == kept
    assert asked.count("https://yt3.ggpht.com/png") == 1  # downloaded once
    assert acc.image("../registry.json") is None and acc.image("0" * 20) is None and acc.image("") is None


def test_an_unfinished_sign_in_is_swept_and_a_known_account_is_not(accounts):
    kept = accounts.create()
    accounts.found(kept["id"], {"channel_id": UC1})
    left = accounts.create()
    (accounts.root / left["id"] / "profile").mkdir()
    accounts.sweep()
    assert not (accounts.root / left["id"]).exists() and accounts.get(kept["id"]) is not None
    assert accounts.get("../x") is None
    with pytest.raises(ValueError):
        accounts.folder("../../etc")


def test_the_sign_in_of_before_is_adopted_where_it_is(accounts):
    old = accounts.legacy_profile()
    assert old == accounts.root.parent / "yt-chrome-profile" and accounts.legacy() is None
    old.mkdir(parents=True)
    (old / "Local State").write_text("{}")
    assert accounts.legacy() == old
    rec = accounts.create(profile=old)
    accounts.found(rec["id"], {"channel_id": UC1, "name": "Old channel"})
    assert accounts.profile_path(rec["id"]) == old and accounts.legacy() is None  # it is in the list now
    assert Accounts.public(accounts.list()[0])["adopted"] is True
    accounts.remove(rec["id"])
    assert (old / "Local State").exists() and accounts.legacy() == old  # the old profile is never deleted


def test_uploads_use_the_named_profile_else_the_account_that_posts_else_the_old_one(
    accounts, monkeypatch, tmp_path
):
    assert youtube_browser.profile_dir() == youtube_browser.DEFAULT_PROFILE
    rec = accounts.create()
    assert youtube_browser.profile_dir() == youtube_browser.DEFAULT_PROFILE  # nobody signed in to it yet
    accounts.found(rec["id"], {"channel_id": UC1})
    assert youtube_browser.profile_dir() == accounts.root / rec["id"] / "profile"
    monkeypatch.setenv("YT_BROWSER_PROFILE", str(tmp_path / "mine"))
    assert youtube_browser.profile_dir() == tmp_path / "mine"


# -- what the window is on -----------------------------------------------------------------------------------
def test_signed_in_means_studio_shows_a_channel():
    sign_in = (
        "https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fstudio.youtube.com%2Fchannel%2F"
        + UC1
    )
    assert youtube_login.channel_of([sign_in]) is None and youtube_login.asks_for_sign_in([sign_in])
    assert youtube_login.channel_of(["https://studio.youtube.com/"]) is None
    assert youtube_login.channel_of(["https://evil.example/studio.youtube.com/channel/" + UC1]) is None
    assert (
        youtube_login.channel_of(["about:blank", f"https://studio.youtube.com/channel/{UC1}/videos"]) == UC1
    )
    assert youtube_login.channel_of([f"https://studio.youtube.com/channel/{UC1}"]) == UC1
    assert not youtube_login.asks_for_sign_in([f"https://studio.youtube.com/channel/{UC1}"])


def test_the_channel_is_read_from_its_public_page():
    assert youtube_login.channel_from_html(PAGE, UC1) == {
        "channel_id": UC1,
        "name": "Clip & Vault",
        "picture_url": "https://yt3.googleusercontent.com/abc=s240-c-k-c0x00ffffff-no-rj",
        "handle": "@clipvault",
        "subscribers": 12400,
    }
    assert youtube_login.channel_from_html("<html>nothing</html>", UC1) == {"channel_id": UC1}
    millions = youtube_login.channel_from_html('"1.23M subscribers"', UC1)
    assert millions["subscribers"] == 1_230_000
    assert youtube_login.channel_from_html('"1 subscriber"', UC1)["subscribers"] == 1


# -- one sign-in, on a stand-in browser ----------------------------------------------------------------------
class FakeChrome:
    """What youtube_login asks of a browser. ``script`` is what the window shows, poll by poll; the last
    entry stays. An entry that is an empty list is a closed window."""

    def __init__(self, script, page=PAGE, opens=True):
        self.script, self.page, self.opens = list(script), page, opens
        self.log, self.read_urls, self.closed = [], [], False

    async def open(self):
        self.log.append("open")
        if not self.opens:
            raise RuntimeError(
                "Chromium distribution 'chrome' is not found at /Applications/Google Chrome.app"
            )

    async def goto(self, url):
        self.log.append(f"goto {url}")

    def urls(self):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    async def read(self, url):
        self.read_urls.append(url)
        if self.page is None:
            raise RuntimeError("no page")
        return self.page

    async def close(self):
        self.closed = True
        self.log.append("close")


GOOGLE = ["https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fstudio.youtube.com%2F"]
STUDIO = [f"https://studio.youtube.com/channel/{UC1}"]


def _run(accounts, account_id, chrome, **kw):
    kw.setdefault("poll_s", 0.01)
    return asyncio.run(youtube_login.run(account_id, accounts=accounts, browser=chrome, **kw))


def _said(accounts, account_id):
    return json.loads(accounts.status_path(account_id).read_text())


def test_a_sign_in_ends_with_the_channel_kept_and_chrome_closed_first(accounts, monkeypatch):
    rec = accounts.create()
    chrome = FakeChrome([GOOGLE, GOOGLE, STUDIO])
    phases = []
    real_set = youtube_login.Status.set

    def watch(self, phase, **more):
        phases.append((phase, chrome.closed))
        real_set(self, phase, **more)

    monkeypatch.setattr(youtube_login.Status, "set", watch)
    assert _run(accounts, rec["id"], chrome) == "done"
    assert [p for p, _ in phases] == ["opening", "waiting", "signed_in", "reading", "done"]
    assert phases[-1] == ("done", True) and phases[-2] == (
        "reading",
        False,
    )  # the session is saved before "done"
    assert chrome.log == ["open", "goto https://studio.youtube.com", "close"]
    assert chrome.read_urls == [f"https://www.youtube.com/channel/{UC1}?hl=en"]
    said = _said(accounts, rec["id"])
    assert said["phase"] == "done" and said["mode"] == "signin" and said["account"] == rec["id"]
    assert said["channel"]["name"] == "Clip & Vault" and said["channel"]["posting"] is True
    assert said["channel"]["picture"].startswith("/connect/image/")
    (row,) = accounts.list()
    assert (row["channel_id"], row["handle"], row["subscribers"], row["state"]) == (
        UC1,
        "@clipvault",
        12400,
        "ready",
    )


def test_a_channel_whose_page_cannot_be_read_is_still_an_account(accounts):
    rec = accounts.create()
    assert _run(accounts, rec["id"], FakeChrome([STUDIO], page=None)) == "done"
    (row,) = accounts.list()
    assert row["channel_id"] == UC1 and "name" not in row and row["state"] == "ready"


@pytest.mark.parametrize(
    ("script", "kw", "end"),
    [
        ([GOOGLE, GOOGLE, []], {}, "closed"),  # the window was closed by hand
        ([GOOGLE], {"timeout_s": 0.05}, "timeout"),
        ([["https://studio.youtube.com/"]], {"timeout_s": 0.05}, "timeout"),  # signed in, but no channel
    ],
)
def test_a_sign_in_that_ends_early_keeps_nothing_and_says_why(accounts, script, kw, end):
    rec = accounts.create()
    chrome = FakeChrome(script)
    assert _run(accounts, rec["id"], chrome, **kw) == end
    assert _said(accounts, rec["id"])["phase"] == end and chrome.closed and chrome.read_urls == []
    assert accounts.list() == [] and accounts.get(rec["id"])["pending"] is True


def test_a_cancel_closes_chrome_and_keeps_nothing(accounts):
    rec = accounts.create()
    chrome = FakeChrome([GOOGLE])

    async def go():
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        return await youtube_login.run(
            rec["id"], accounts=accounts, browser=chrome, poll_s=0.01, cancel=cancel
        )

    assert asyncio.run(go()) == "cancelled"
    assert _said(accounts, rec["id"])["phase"] == "cancelled" and chrome.closed and accounts.list() == []


def test_chrome_that_does_not_open_is_said_in_a_plain_sentence(accounts):
    rec = accounts.create()
    assert _run(accounts, rec["id"], FakeChrome([GOOGLE], opens=False)) == "failed"
    said = _said(accounts, rec["id"])
    assert said["message"] == "Google Chrome was not found on this machine. Install Chrome, then try again."
    assert "/Applications" not in json.dumps(said)
    assert "already open" in youtube_login.why_not(RuntimeError("ProcessSingleton: profile in use"))
    with pytest.raises(ValueError):
        _run(accounts, "0123456789ab", FakeChrome([GOOGLE]))


def test_a_check_finds_an_account_signed_out_or_refreshes_it(accounts):
    rec = accounts.create()
    accounts.found(rec["id"], {"channel_id": UC1, "name": "Old name"})
    assert _run(accounts, rec["id"], FakeChrome([GOOGLE]), mode="check") == "signed_out"
    assert accounts.get(rec["id"])["state"] == "signin" and accounts.get(rec["id"])["name"] == "Old name"
    assert (
        _run(accounts, rec["id"], FakeChrome([["https://studio.youtube.com/"], STUDIO]), mode="check")
        == "done"
    )
    row = accounts.get(rec["id"])
    assert row["state"] == "ready" and row["name"] == "Clip & Vault"
    stuck = FakeChrome([["https://studio.youtube.com/"]])
    assert _run(accounts, rec["id"], stuck, mode="check", timeout_s=0.05) == "failed"
    assert "did not load in time" in _said(accounts, rec["id"])["message"]


def test_a_channel_that_is_already_kept_is_not_added_twice(accounts):
    first = accounts.create()
    accounts.found(first["id"], {"channel_id": UC1, "name": "Kept"})
    second = accounts.create()
    assert _run(accounts, second["id"], FakeChrome([STUDIO])) == "duplicate"
    said = _said(accounts, second["id"])
    assert said["channel"]["name"] == "Kept" and [r["id"] for r in accounts.list()] == [first["id"]]
    # signing the first one in again is not a duplicate of itself
    assert _run(accounts, first["id"], FakeChrome([STUDIO])) == "done"
