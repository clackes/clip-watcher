"""Studio pop-ups over the upload dialog (21 Nov: a backdrop took the Publish click for 30 s)."""

import asyncio

import pytest

from clipwatcher.publish import youtube_browser as yb


class Button:
    def __init__(self, blocked_clicks=0):
        self.blocked = blocked_clicks
        self.clicks = self.js = 0

    async def click(self, timeout=None):
        if self.blocked:
            self.blocked -= 1
            raise TimeoutError("Locator.click: Timeout exceeded.\nbackdrop intercepts pointer events")
        self.clicks += 1

    async def evaluate(self, js):
        self.js += 1


class Page:
    def __init__(self, dismissed=()):
        self.dismissed = list(dismissed)
        self.shots = []
        self.evals = 0

    async def evaluate(self, js):
        self.evals += 1
        return self.dismissed

    async def screenshot(self, path, full_page=True):
        self.shots.append(path)


def run(coro):
    return asyncio.run(coro)


def test_free_button_is_clicked_once(tmp_path):
    page, b = Page(), Button()
    run(yb._press(page, b, shots=tmp_path, name="publish"))
    assert (b.clicks, b.js, page.evals, page.shots) == (1, 0, 0, [])


def test_popup_is_dismissed_then_clicked(tmp_path):
    page, b = Page(dismissed=["Got it"]), Button(blocked_clicks=1)
    run(yb._press(page, b, shots=tmp_path, name="publish"))
    assert (b.clicks, b.js, page.evals) == (1, 0, 1)
    assert len(page.shots) == 1 and "publish-blocked" in page.shots[0]


def test_backdrop_that_stays_gets_a_page_click(tmp_path):
    page, b = Page(), Button(blocked_clicks=5)
    run(yb._press(page, b, shots=None, name="publish"))
    assert (b.clicks, b.js) == (0, 1)


class Done:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def is_visible(self):
        self.page.looks += 1
        return self.page.looks >= self.page.shown_after


class StudioPage(Page):
    def __init__(self, shown_after):
        super().__init__()
        self.looks, self.shown_after, self.texts = 0, shown_after, []

    def get_by_text(self, pattern):
        self.texts.append(pattern)
        return Done(self)


def test_published_waits_for_studio_to_say_so(monkeypatch):
    monkeypatch.setattr(yb.asyncio, "sleep", _no_sleep)
    page = StudioPage(shown_after=3)
    run(yb._published(page))
    assert page.looks == 3
    for said in ("Video published", "Video processing", "Video saved"):
        assert page.texts[0].match(said)
    assert not page.texts[0].match("Publish")


def test_publish_that_did_not_take_is_an_error(monkeypatch):
    monkeypatch.setattr(yb.asyncio, "sleep", _no_sleep)
    with pytest.raises(RuntimeError, match="did not say"):
        run(yb._published(StudioPage(shown_after=10**9), timeout_s=0.05))


async def _no_sleep(_s):
    return None


def test_shots_default_and_off(monkeypatch):
    monkeypatch.delenv("YT_BROWSER_SHOTS", raising=False)
    assert yb._shots_dir() == yb.config.ROOT / "work" / "yt_shots"
    monkeypatch.setenv("YT_BROWSER_SHOTS", "")
    assert yb._shots_dir() is None
