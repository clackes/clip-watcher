"""One-time YouTube Studio login for the browser publisher.

Opens the user's real Chrome with a dedicated ClipWatcher profile. Sign in with
the Google account that owns the ClipVault channel, pick that channel, then
press Enter here to close. The profile is reused by publish/youtube_browser.py.

    python scripts/yt_browser_login.py

Override the profile dir with YT_BROWSER_PROFILE. Without it this opens the profile uploads use: the
account that posts when the companion app keeps accounts (its Connect place does this sign-in step by step,
for several accounts), else ~/.clipwatcher/yt-chrome-profile.
"""

from __future__ import annotations

import asyncio

from clipwatcher.publish.youtube_browser import STUDIO_URL, profile_dir


async def main() -> None:
    from patchright.async_api import async_playwright

    pdir = profile_dir()
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
    asyncio.run(main())
