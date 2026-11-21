"""Browser-driven YouTube publisher.

Opens the user's real Chrome via patchright with a persistent profile and walks
through YouTube Studio's upload UI. Approve on Telegram triggers this when
``publish.youtube_mode: browser`` in config/global.yaml.

Sign in once in the companion app (Connect, YouTube: one Chrome profile per account under
``~/.clipwatcher/youtube/``, the upload uses the account that posts) or with
``python scripts/yt_browser_login.py`` (the single profile ``~/.clipwatcher/yt-chrome-profile``,
used while no account is kept). ``YT_BROWSER_PROFILE`` overrides both. Screenshots on failure go to
``YT_BROWSER_SHOTS``, else work/yt_shots.

DRY_RUN=1 skips the browser and returns a ``dryrun-...`` id, matching
``publish/youtube.py`` behaviour. Set ``YT_BROWSER_FORCE=1`` to upload even
under DRY_RUN (then the visibility is forced to private).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .. import config
from ..queue.db import APPROVED, ClipDB
from .youtube import NotApproved, build_metadata, failure_recorded, record_url

log = logging.getLogger(__name__)

STUDIO_URL = "https://studio.youtube.com"
DEFAULT_PROFILE = Path.home() / ".clipwatcher" / "yt-chrome-profile"
PROFILES_ROOT = Path.home() / ".clipwatcher" / "yt-profiles"  # one saved Chrome session per account username
_URL_RE = re.compile(r"(?:shorts/|youtu\.be/|watch\?v=)([A-Za-z0-9_-]{6,})")


def profile_dir(account: str | None = None) -> Path:
    """The Chrome profile uploads go through: YT_BROWSER_PROFILE when it is set; else the named account's
    profile (account, or YT_BROWSER_ACCOUNT=<username> -> ~/.clipwatcher/yt-profiles/<username>, the
    server's capybarasking); else the account that posts (publish/youtube_accounts.py, chosen in the
    companion app); else the single profile of before."""
    p = os.environ.get("YT_BROWSER_PROFILE")
    if p:
        return Path(p).expanduser()
    account = account or os.environ.get("YT_BROWSER_ACCOUNT")
    if account:
        return PROFILES_ROOT / re.sub(r"[^A-Za-z0-9._@-]", "_", account.strip())
    from . import youtube_accounts

    return youtube_accounts.posting_profile() or DEFAULT_PROFILE


def _shots_dir() -> Path | None:
    """Where a failed upload leaves a picture of Studio: YT_BROWSER_SHOTS, else work/yt_shots (empty
    YT_BROWSER_SHOTS turns it off)."""
    s = os.environ.get("YT_BROWSER_SHOTS")
    if s is None:
        return config.ROOT / "work" / "yt_shots"
    return Path(s).expanduser() if s else None


async def _shot(page, shots: Path | None, name: str) -> None:
    if not shots:
        return
    try:
        await page.screenshot(path=str(shots / f"yt-{name}-{uuid.uuid4().hex[:8]}.png"), full_page=True)
    except Exception:
        log.exception("screenshot failed")


# Studio's own pop-ups (a tip, a notice, a survey) open over the upload dialog and take every click
# (owner's first real upload, 21 Nov: a backdrop held Publish for 30 s). Dismiss them by their own buttons,
# never one inside the upload dialog, whose Close would throw the upload away.
_DISMISS_JS = r"""() => {
  const up = document.querySelector('ytcp-uploads-dialog');
  const words = /^(got it|dismiss|ok|okay|no thanks|not now|skip|maybe later|close)$/i;
  const done = [];
  const sel = 'tp-yt-paper-dialog, ytcp-dialog, [role="dialog"], [role="alertdialog"]';
  for (const d of document.querySelectorAll(sel)) {
    if (up && (up.contains(d) || d.contains(up))) continue;
    if (!d.getClientRects().length) continue;
    for (const b of d.querySelectorAll('button, ytcp-button, tp-yt-paper-button, ytcp-icon-button')) {
      const t = (b.getAttribute('aria-label') || b.innerText || '').trim();
      if (words.test(t)) { b.click(); done.push(t); break; }
    }
  }
  return done;
}"""


async def _press(page, button, *, shots: Path | None, name: str, timeout_ms: int = 15_000) -> None:
    """Click a Studio button that a pop-up may be covering: a real click; else dismiss the pop-ups and click
    again; else click the button itself from the page (what the pop-up's backdrop does not stop)."""
    try:
        await button.click(timeout=timeout_ms)
        return
    except Exception as e:
        log.warning("youtube: %s click blocked (%s): dismissing pop-ups", name, str(e).splitlines()[0])
        await _shot(page, shots, f"{name}-blocked")
    dismissed = await page.evaluate(_DISMISS_JS)
    if dismissed:
        log.info("youtube: dismissed %s", dismissed)
    try:
        await button.click(timeout=timeout_ms)
        return
    except Exception:
        log.warning("youtube: %s still blocked: clicking it from the page", name)
    await button.evaluate("b => b.click()")


def _select_all_key() -> str:
    """Select-all in the focused text box: Cmd+A on macOS, Ctrl+A on Windows and Linux (Meta+A selects
    nothing there, so Studio's prefilled title stayed and ours was typed behind it)."""
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


async def _upload(
    video: Path,
    title: str,
    description: str,
    *,
    private: bool,
    timeout_s: int = 900,
    on_video_id: Callable[[str], None] | None = None,
) -> str:
    from patchright.async_api import async_playwright

    shots = _shots_dir()
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    pdir = profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(pdir),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
            if "accounts.google.com" in page.url:
                raise RuntimeError(
                    "YouTube profile is not signed in — run `python scripts/yt_browser_login.py` once."
                )

            # Create → Upload videos opens the upload dialog. Push the file into
            # its hidden <input type="file"> directly: more reliable than the OS
            # chooser and lets Studio pick it up without a user gesture.
            await page.get_by_role("button", name="Create", exact=True).click()
            await page.get_by_role("menuitem", name=re.compile(r"Upload videos?", re.I)).click()
            file_input = page.locator('input[type="file"]').last
            await file_input.wait_for(state="attached", timeout=30_000)
            await file_input.set_input_files(str(video))

            # Title — Studio uses two ytcp-social-suggestions-textbox widgets.
            # First one is Title, second is Description. Each contains #textbox.
            boxes = page.locator("ytcp-social-suggestions-textbox #textbox")
            await boxes.first.wait_for(timeout=120_000)
            await boxes.first.click()
            await page.keyboard.press(_select_all_key())
            await page.keyboard.press("Delete")
            await page.keyboard.type(title[:95])

            await boxes.nth(1).click()
            await page.keyboard.type(description[:4900])

            # Audience: Not made for kids.
            await page.get_by_role("radio", name=re.compile(r"No,? it'?s not made for kids", re.I)).click()

            # Studio stamps the assigned id on the dialog as video-id="..."
            # as soon as the backend accepts the file, well before the upload
            # finishes. Read it directly: the share link on the page itself
            # isn't a plain <a>, and the Studio shell carries unrelated
            # youtube.com/watch anchors we must not match.
            dialog_with_id = page.locator("ytcp-uploads-dialog[video-id]").first
            await dialog_with_id.wait_for(state="attached", timeout=120_000)
            vid = await dialog_with_id.get_attribute("video-id") or ""
            if not vid:
                raise RuntimeError("Studio did not assign a video-id")
            if on_video_id is not None:
                on_video_id(vid)
            href = f"https://youtube.com/shorts/{vid}"

            # Details → Video elements → Checks → Visibility: three Next clicks.
            for _ in range(3):
                nxt = page.get_by_role("button", name="Next", exact=True)
                await _press(page, nxt, shots=shots, name="next")
                await asyncio.sleep(0.8)

            label = "Private" if private else "Public"
            radio = page.get_by_role("radio", name=re.compile(rf"^{label}$"))
            await _press(page, radio, shots=shots, name="visibility")

            # Publish (public) or Save (private). Studio applies this even if
            # the upload is still finishing.
            publish = page.get_by_role("button", name=re.compile(r"^(Publish|Save)$"))
            await _press(page, publish, shots=shots, name="publish")
            await _published(page)

            # Dismiss the "done" dialog if one appears.
            try:
                await page.get_by_role("button", name=re.compile(r"^Close$", re.I)).click(timeout=30_000)
            except Exception:
                pass
            return href
        except Exception:
            await _shot(page, shots, "fail")
            raise
        finally:
            await ctx.close()


async def _published(page, timeout_s: float = 90.0) -> None:
    """Publish went through only when Studio says so ("Video published", or "Video saved" for a private
    one): a click that did nothing must not mark the clip PUBLISHED. Not the Publish button going away:
    on the server's first uploads (21 Nov) the video was out and the button never counted as hidden."""
    done = page.get_by_text(re.compile(r"^\s*Video (published|saved)\s*$", re.I)).first
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        try:
            if await done.is_visible():
                return
        except Exception:
            pass
        await asyncio.sleep(1.0)
    raise RuntimeError("Publish was pressed but Studio did not say the video was published")


@contextlib.contextmanager
def _profile_lock(wait_s: float = 1800, stale_s: float = 1800):
    """One upload at a time per Chrome profile: auto-publish (live lane) and Approve (Telegram bot) run in
    different processes, and a second Chrome on a profile in use fails. A lock older than stale_s is a
    crashed upload's and is taken over."""
    lock = profile_dir() / ".cw-upload.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    t_end = time.time() + wait_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > t_end:
                raise RuntimeError("another YouTube upload is still running") from None
            time.sleep(5)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def publish_clip(db: ClipDB, clip_id: str, root: Path | None = None) -> str:
    """Upload an APPROVED clip through real Chrome. Returns watch URL or dryrun id."""
    from ..queue import staging

    state = db.state(clip_id)
    if state != APPROVED:
        raise NotApproved(f"{clip_id} is {state}, not APPROVED")

    root = Path(root) if root else staging.staging_root()
    with failure_recorded(db, clip_id):
        folder = root / "approved" / clip_id
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        row = db.get(clip_id) or {}
        meta.update(json.loads(row.get("meta_json") or "{}"))
        md = build_metadata(meta)
        video = folder / "short_9x16.mp4"

        force = os.environ.get("YT_BROWSER_FORCE") == "1"
        if config.dry_run() and not force:
            vid = f"dryrun-{uuid.uuid4().hex[:8]}"
            db.log("publisher", "youtube_browser_dry_run", clip_id, f"would upload {video.name}")
            return vid

        private = config.dry_run()  # under DRY_RUN with force, still upload private
        with _profile_lock():  # one Chrome per profile: the scheduler and Telegram never upload at once
            url = asyncio.run(
                _upload(
                    video, md["title"], md["description"], private=private,
                    # Studio hands out the id long before the dialog is through: keep it, so a video that got
                    # stuck half way can be found instead of blindly uploaded again
                    on_video_id=lambda yt_id: db.note_upload(clip_id, yt_id),
                )
            )  # fmt: skip
        m = _URL_RE.search(url)
        yt_id = m.group(1) if m else url
        db.set_state(clip_id, "PUBLISHED", actor="publisher", youtube_id=yt_id)
    record_url(db, clip_id, url, root)
    return url
