"""Browser-driven YouTube publisher.

Opens the user's real Chrome via patchright with a persistent profile and walks
through YouTube Studio's upload UI. Approve on Telegram triggers this when
``publish.youtube_mode: browser`` in config/global.yaml.

Sign in once with ``python scripts/yt_browser_login.py``; the resulting profile
(default ``~/.clipwatcher/yt-chrome-profile``) is reused on every upload.
Override with ``YT_BROWSER_PROFILE``. Screenshots on failure go to
``YT_BROWSER_SHOTS`` when set.

DRY_RUN=1 skips the browser and returns a ``dryrun-...`` id, matching
``publish/youtube.py`` behaviour. Set ``YT_BROWSER_FORCE=1`` to upload even
under DRY_RUN (then the visibility is forced to private).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

from .. import config
from ..queue.db import APPROVED, ClipDB
from .youtube import NotApproved, build_metadata

log = logging.getLogger(__name__)

STUDIO_URL = "https://studio.youtube.com"
DEFAULT_PROFILE = Path.home() / ".clipwatcher" / "yt-chrome-profile"
_URL_RE = re.compile(r"(?:shorts/|youtu\.be/|watch\?v=)([A-Za-z0-9_-]{6,})")


def profile_dir() -> Path:
    p = os.environ.get("YT_BROWSER_PROFILE")
    return Path(p).expanduser() if p else DEFAULT_PROFILE


def _shots_dir() -> Path | None:
    s = os.environ.get("YT_BROWSER_SHOTS")
    return Path(s).expanduser() if s else None


async def _upload(
    video: Path,
    title: str,
    description: str,
    *,
    private: bool,
    timeout_s: int = 900,
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

            # Create → Upload videos (opens the native file chooser).
            async with page.expect_file_chooser(timeout=60_000) as fc_info:
                await page.get_by_role("button", name="Create", exact=True).click()
                await page.get_by_role("menuitem", name=re.compile(r"Upload videos?", re.I)).click()
            chooser = await fc_info.value
            await chooser.set_files(str(video))

            # Title — Studio uses two ytcp-social-suggestions-textbox widgets.
            # First one is Title, second is Description. Each contains #textbox.
            boxes = page.locator("ytcp-social-suggestions-textbox #textbox")
            await boxes.first.wait_for(timeout=120_000)
            await boxes.first.click()
            await page.keyboard.press("Meta+A")
            await page.keyboard.press("Delete")
            await page.keyboard.type(title[:95])

            await boxes.nth(1).click()
            await page.keyboard.type(description[:4900])

            # Audience: Not made for kids.
            await page.get_by_role("radio", name=re.compile(r"No,? it'?s not made for kids", re.I)).click()

            # Details → Video elements → Checks → Visibility: three Next clicks.
            for _ in range(3):
                await page.get_by_role("button", name="Next").click()
                await asyncio.sleep(0.8)

            label = "Private" if private else "Public"
            await page.get_by_role("radio", name=re.compile(rf"^{label}$")).click()

            # Publish (public) or Save (private).
            await page.get_by_role("button", name=re.compile(r"^(Publish|Save)$")).click()

            # Capture the share link from the done dialog.
            link = page.locator(
                'a[href*="youtu.be/"], a[href*="youtube.com/shorts/"], a[href*="youtube.com/watch"]'
            ).first
            await link.wait_for(timeout=timeout_s * 1000)
            href = await link.get_attribute("href") or ""
            if not href:
                raise RuntimeError("could not read video URL from Studio share dialog")

            # Close the dialog so the next run starts clean.
            try:
                await page.get_by_role("button", name=re.compile(r"^Close$", re.I)).click(timeout=5000)
            except Exception:
                pass
            return href
        except Exception:
            if shots:
                try:
                    await page.screenshot(
                        path=str(shots / f"yt-fail-{uuid.uuid4().hex[:8]}.png"), full_page=True
                    )
                except Exception:
                    log.exception("screenshot failed")
            raise
        finally:
            await ctx.close()


def publish_clip(db: ClipDB, clip_id: str, root: Path | None = None) -> str:
    """Upload an APPROVED clip through real Chrome. Returns watch URL or dryrun id."""
    from ..queue import staging

    state = db.state(clip_id)
    if state != APPROVED:
        raise NotApproved(f"{clip_id} is {state}, not APPROVED")

    root = Path(root) if root else staging.DEFAULT_ROOT
    folder = root / "approved" / clip_id
    meta = json.loads((folder / "meta.json").read_text())
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
    url = asyncio.run(_upload(video, md["title"], md["description"], private=private))
    m = _URL_RE.search(url)
    yt_id = m.group(1) if m else url
    db.set_state(clip_id, "PUBLISHED", actor="publisher", youtube_id=yt_id)
    staging.update_published(clip_id, "youtube", url, root)
    return url
