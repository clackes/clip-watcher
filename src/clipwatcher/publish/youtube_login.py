"""Sign one account in to YouTube in real Chrome, or check that it still is.

    python -m clipwatcher.publish.youtube_login --account <id> [--check] [--timeout 600]

Chrome is opened exactly as the uploader opens it (publish/youtube_browser.py: patchright, a persistent
profile, the installed Chrome, a visible window) on YouTube Studio, with the profile of the account. A person
signs in there; this process only watches which address the window is on. Signed in means Studio shows a
channel. The channel's id is read from that address, its name, handle, picture and subscribers from the
channel's public page, and Chrome is closed by this process so the session is on disk before "done" is said.

What it has reached is written to the account's status.json, which the companion app reads:
opening, waiting, signed_in, reading, then one of done, closed (the window was closed), cancelled (SIGTERM or
SIGINT), timeout, signed_out (a check found the account signed out), duplicate (the channel is already
kept), failed (with a plain reason).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import signal
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .youtube_accounts import CHANNEL_ID, SIGN_IN, Accounts, _write_json

STUDIO_URL = "https://studio.youtube.com"
TIMEOUT_S = 600.0  # how long a person gets to sign in
CHECK_S = 40.0  # how long a check waits for Studio to show the channel
ENDS = ("done", "closed", "cancelled", "timeout", "signed_out", "duplicate", "failed")
_CHANNEL_PATH = re.compile(r"/channel/(" + CHANNEL_ID.pattern + r")(?:[/?#]|$)")
_META = r'<meta\s+(?:property|name)="{}"\s+content="([^"]*)"'
_HANDLE = re.compile(r'"vanityChannelUrl":"https?:(?:\\?/){2}www\.youtube\.com\\?/@([^"\\/?&<\s]{3,60})"')
_SUBS = re.compile(r'"([\d.,]+)\s?([KMB])?\s+subscribers?"')


class _Cancelled(Exception):
    pass


# -- what the window is on -----------------------------------------------------------------------------------
def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def channel_of(urls: list[str]) -> str | None:
    """The channel a signed-in Studio window shows, from its address (studio.youtube.com/channel/UC...)."""
    for url in urls:
        if _host(url) == "studio.youtube.com":
            m = _CHANNEL_PATH.search(urllib.parse.urlsplit(url).path)
            if m:
                return m.group(1)
    return None


def asks_for_sign_in(urls: list[str]) -> bool:
    return any(_host(u) == "accounts.google.com" for u in urls)


def channel_from_html(page: str, channel_id: str) -> dict[str, Any]:
    """Name, handle, picture and subscribers from a channel's public page (asked for in English)."""
    out: dict[str, Any] = {"channel_id": channel_id}

    def meta(name: str) -> str:
        m = re.search(_META.format(re.escape(name)), page)
        return html.unescape(m.group(1)).strip() if m else ""

    name, picture = meta("og:title"), meta("og:image")
    if name:
        out["name"] = name[:120]
    if picture.startswith("https://"):
        out["picture_url"] = re.sub(r"=s\d+-", "=s240-", picture)  # a size the page shows, not 900 px
    m = _HANDLE.search(page)
    if m:
        out["handle"] = "@" + urllib.parse.unquote(m.group(1))[:60]
    m = _SUBS.search(page)
    if m:
        try:
            n = float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2) or ""]
            out["subscribers"] = int(round(n))
        except ValueError:
            pass
    return out


# -- Chrome --------------------------------------------------------------------------------------------------
class Chrome:
    """Real Chrome on one profile, opened as the uploader opens it. The few things this module asks of a
    browser, so the tests can stand a fake in its place."""

    def __init__(self, profile: Path):
        self.profile = profile
        self._pw: Any = None
        self._ctx: Any = None
        self._gone = False

    async def open(self) -> None:
        from patchright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self._ctx.on("close", lambda *_: setattr(self, "_gone", True))

    async def goto(self, url: str) -> None:
        page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    def urls(self) -> list[str]:
        """The addresses of the open tabs; none when the window was closed or Chrome was quit."""
        if self._gone or self._ctx is None:
            return []
        try:
            return [p.url for p in self._ctx.pages if not p.is_closed()]
        except Exception:  # noqa: BLE001 - Chrome went away while we looked
            return []

    async def read(self, url: str) -> str:
        page = await self._ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return await page.content()
        finally:
            await page.close()

    async def close(self) -> None:
        for step in (lambda: self._ctx and self._ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                pending = step()
                if pending is not None:
                    await asyncio.wait_for(pending, timeout=20)
            except Exception:  # noqa: BLE001 - already gone is as good as closed
                pass


def why_not(e: BaseException) -> str:
    """A plain sentence for why Chrome did not open."""
    text = str(e).lower()
    if isinstance(e, ImportError):
        return "The browser helper is not installed on this machine (pip install patchright)."
    if "executable doesn't exist" in text or "is not found" in text or "not installed" in text:
        return "Google Chrome was not found on this machine. Install Chrome, then try again."
    if "processsingleton" in text or "in use" in text or "singletonlock" in text:
        return "Chrome is already open with this account. Close that window, then try again."
    return "Chrome could not be opened. Close other Chrome windows of this account and try again."


# -- one sign-in or check ------------------------------------------------------------------------------------
class Status:
    """What the app reads while this runs."""

    def __init__(self, path: Path, account_id: str, mode: str, timeout_s: float):
        self.path, self.started = path, time.time()
        self.base = {"account": account_id, "mode": mode, "pid": os.getpid(), "started": self.started}
        self.deadline = self.started + timeout_s
        self.phase = ""

    def set(self, phase: str, **more: Any) -> None:
        self.phase = phase
        _write_json(
            self.path, {**self.base, "phase": phase, "at": time.time(), "deadline": self.deadline, **more}
        )


async def _unless(cancel: asyncio.Event, work: Any) -> Any:
    """Await ``work`` unless a cancel comes first."""
    task, stop = asyncio.ensure_future(work), asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        raise _Cancelled
    finally:
        for t in (task, stop):
            if not t.done():
                t.cancel()


async def run(
    account_id: str,
    *,
    mode: str = "signin",
    accounts: Accounts | None = None,
    browser: Any = None,
    timeout_s: float | None = None,
    poll_s: float = 1.0,
    cancel: asyncio.Event | None = None,
) -> str:
    """Open Chrome on the account's profile and follow it to an end. Returns the last phase (one of ENDS)."""
    accounts = accounts or Accounts()
    cancel = cancel or asyncio.Event()
    check = mode == "check"
    timeout_s = (CHECK_S if check else TIMEOUT_S) if timeout_s is None else timeout_s
    was = accounts.get(account_id)
    if was is None:
        raise ValueError("No such account.")
    status = Status(accounts.status_path(account_id), account_id, mode, timeout_s)
    chrome = browser or Chrome(accounts.profile_path(account_id))
    status.set("opening")
    end, more = "failed", {}
    try:
        try:
            await _unless(cancel, chrome.open())
        except _Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - said to the person as a sentence
            end, more = "failed", {"message": why_not(e), "detail": type(e).__name__}
            return end
        end, more = await _follow(chrome, status, accounts, account_id, was, cancel, check, timeout_s, poll_s)
    except _Cancelled:
        end, more = "cancelled", {}
    except Exception as e:  # noqa: BLE001 - whatever went wrong, Chrome is closed and the page is told
        closed = not chrome.urls()
        end = "closed" if closed else "failed"
        more = {} if closed else {"message": "Something went wrong in Chrome.", "detail": type(e).__name__}
    finally:
        await chrome.close()  # the session is written to the profile here, before the end is said
        status.set(end, **more)
    return end


async def _follow(
    chrome: Any,
    status: Status,
    accounts: Accounts,
    account_id: str,
    was: dict[str, Any],
    cancel: asyncio.Event,
    check: bool,
    timeout_s: float,
    poll_s: float,
) -> tuple[str, dict[str, Any]]:
    try:
        await _unless(cancel, chrome.goto(STUDIO_URL))
    except _Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        if not chrome.urls():
            return "closed", {}
        return "failed", {
            "message": "YouTube could not be opened. Check the internet connection and try again.",
            "detail": type(e).__name__,
        }
    status.set("waiting")
    until, waited = time.monotonic() + timeout_s, 0.0
    while True:
        urls = chrome.urls()
        if not urls:
            return "closed", {}
        channel_id = channel_of(urls)
        if channel_id:
            break
        if check and asks_for_sign_in(urls):
            accounts.update(account_id, state=SIGN_IN, checked=accounts.now())
            return "signed_out", {}
        if time.monotonic() >= until:
            if check:
                return "failed", {"message": "YouTube Studio did not load in time. Try again."}
            return "timeout", {}
        # signed in to Google, yet Studio shows no channel: the account has none
        no_channel = not check and waited > 20 and not asks_for_sign_in(urls)
        if no_channel and status.phase != "waiting_channel":
            status.set("waiting", note="no_channel")
            status.phase = "waiting_channel"
        try:
            await asyncio.wait_for(cancel.wait(), timeout=poll_s)
            raise _Cancelled
        except TimeoutError:
            waited += poll_s
    status.set("signed_in", channel_id=channel_id)
    status.set("reading", channel_id=channel_id)
    channel: dict[str, Any] = {"channel_id": channel_id}
    try:
        page = await _unless(cancel, chrome.read(f"https://www.youtube.com/channel/{channel_id}?hl=en"))
        channel = channel_from_html(page, channel_id)
    except _Cancelled:
        raise
    except Exception:  # noqa: BLE001 - the sign-in worked; a channel without its name is still an account
        pass
    twin = accounts.same_channel(account_id, channel_id)
    if twin is not None and was.get("pending"):
        return "duplicate", {"channel": Accounts.public(twin)}
    rec = accounts.found(account_id, channel)
    return "done", {"channel": Accounts.public({**rec, "posting": accounts.posting_id() == account_id})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.publish.youtube_login", description=__doc__)
    ap.add_argument("--account", required=True, help="the account's id (the companion app makes it)")
    ap.add_argument("--check", action="store_true", help="only confirm the account is still signed in")
    ap.add_argument("--timeout", type=float, default=None, help="seconds to wait for the sign-in")
    a = ap.parse_args(argv)

    async def go() -> str:
        cancel = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, cancel.set)
            except (NotImplementedError, RuntimeError):  # Windows: Ctrl-C arrives as KeyboardInterrupt
                pass
        return await run(a.account, mode="check" if a.check else "signin", timeout_s=a.timeout, cancel=cancel)

    try:
        end = asyncio.run(go())
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(end, flush=True)
    return 0 if end == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
