"""One-time YouTube Studio login for the browser publisher.

Opens the user's real Chrome with a dedicated ClipWatcher profile. Sign in with
the Google account that owns the ClipVault channel, pick that channel, then
press Enter here to close. The profile is reused by publish/youtube_browser.py.

    python scripts/yt_browser_login.py --account <username>     # -> ~/.clipwatcher/yt-profiles/<username>
    python scripts/yt_browser_login.py --profile-dir D:/profiles/x

Then set YT_BROWSER_ACCOUNT=<username> in .env so the publisher uploads with that saved session.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from clipwatcher.publish.youtube_browser import STUDIO_URL, profile_dir


async def main(pdir: Path) -> None:
    from patchright.async_api import async_playwright

    pdir.mkdir(parents=True, exist_ok=True)
    print(f"profile: {pdir}")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(pdir),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(STUDIO_URL)
        print("Sign in in the Chrome window, confirm you see YouTube Studio for your channel,")
        print("then press Enter here to save the profile and close.")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await ctx.close()
    print("done. patchright profile saved.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", help="username of the YouTube/Google account (profile per account)")
    ap.add_argument("--profile-dir", type=Path, help="explicit Chrome profile directory")
    a = ap.parse_args()
    asyncio.run(main(a.profile_dir.expanduser() if a.profile_dir else profile_dir(a.account)))
