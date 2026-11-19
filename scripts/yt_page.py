"""Run a small async step file against YouTube Studio in the bot's signed-in Chrome profile.

    python scripts/yt_page.py steps.py          # steps.py defines: async def run(page, shot) -> None

shot(name) saves work/yt_shots/<name>.png. Holds the upload lock so it never races a clip upload.
Used for one-off channel admin (owner, 19 Nov: archive old videos, new banner).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from clipwatcher.publish.youtube_browser import _profile_lock, profile_dir

SHOTS = Path("work/yt_shots")


async def main(step_file: Path) -> None:
    from patchright.async_api import async_playwright

    spec = importlib.util.spec_from_file_location("steps", step_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    SHOTS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir()), channel="chrome", headless=False,
            viewport={"width": 1400, "height": 900},
        )  # fmt: skip
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def shot(name: str) -> None:
            await page.screenshot(path=str(SHOTS / f"{name}.png"))
            print("shot", name, page.url)

        try:
            await mod.run(page, shot)
        finally:
            await ctx.close()


if __name__ == "__main__":
    with _profile_lock(wait_s=900):
        asyncio.run(main(Path(sys.argv[1])))
