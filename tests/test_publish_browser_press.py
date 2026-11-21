"""Studio pop-ups over the upload dialog (21 Nov: a backdrop took the Publish click for 30 s)."""

import asyncio

import pytest

from clipwatcher.publish import youtube_browser as yb


class Button:
    def __init__(self, blocked_clicks=0, hides=True):
        self.blocked = blocked_clicks
        self.clicks = self.js = 0
        self.hides = hides

    async def click(self, timeout=None):
        if self.blocked:
            self.blocked -= 1
            raise TimeoutError("Locator.click: Timeout exceeded.\nbackdrop intercepts pointer events")
        self.clicks += 1

    async def evaluate(self, js):
        self.js += 1

    async def wait_for(self, state=None, timeout=None):
        if not self.hides:
            raise TimeoutError("still visible")


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


def test_publish_that_did_not_take_is_an_error():
    run(yb._published(Button()))
    with pytest.raises(RuntimeError, match="did not publish"):
        run(yb._published(Button(hides=False)))


def test_shots_default_and_off(monkeypatch):
    monkeypatch.delenv("YT_BROWSER_SHOTS", raising=False)
    assert yb._shots_dir() == yb.config.ROOT / "work" / "yt_shots"
    monkeypatch.setenv("YT_BROWSER_SHOTS", "")
    assert yb._shots_dir() is None
