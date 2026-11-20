"""Cheap checks on the files people start the bot with (.env.example, deploy/, scripts/)."""

import importlib
import re
import tomllib

from clipwatcher import config

STARTERS = [
    *(config.ROOT / "deploy").rglob("*.service"),
    config.ROOT / "deploy" / "docker" / "Dockerfile",
    config.ROOT / "deploy" / "docker" / "compose.yaml",
    config.ROOT / "scripts" / "run_live_mac.sh",
    config.ROOT / "scripts" / "run_live_windows.ps1",
]


def test_env_example_has_no_inline_comments():
    """PowerShell's and systemd's loaders took 'KEY=   # hint' as a value made of the hint: the Windows start
    script handed Telegram the comment as bot token."""
    for line in (config.ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            assert name.isidentifier() and "#" not in value and value == value.strip(), line


def test_every_started_module_exists():
    """The worker unit started clipwatcher.queue.worker, which never existed."""
    started = {}
    for path in STARTERS:
        for module in re.findall(r"""-m["',\s]+(clipwatcher(?:\.\w+)+)""", path.read_text()):
            started.setdefault(module, path.name)
    assert {"clipwatcher.live", "clipwatcher.queue.telegram_bot", "clipwatcher.worker"} <= set(started)
    for module, where in started.items():
        assert callable(getattr(importlib.import_module(module), "main", None)), f"{module} in {where}"


def test_systemd_units_can_start():
    units = {p.name: p.read_text() for p in (config.ROOT / "deploy" / "systemd").glob("*.service")}
    assert {"clipwatcher-live.service", "clipwatcher-telegram.service"} <= set(units)
    for name, text in units.items():
        # the secrets (bot token, API keys) have to reach the process as environment variables: nothing in
        # the code reads a credentials file
        assert "EnvironmentFile=/etc/clipwatcher/secrets.env" in text, name
        assert "LoadCredential" not in text and "CLIPWATCHER_CREDENTIALS_FILE" not in text, name
        # only a process that sends READY=1 / WATCHDOG=1 may be Type=notify with a watchdog
        notifies = "clipwatcher.ingest.watcher" in text
        assert ("Type=notify" in text) == notifies and ("WatchdogSec" in text) == notifies, name


def test_docker_image_uploads_through_the_api(monkeypatch):
    """No display in a container: the image installs the Data API publisher and selects it by environment
    (config/global.yaml, copied into the image, says browser)."""
    from clipwatcher.publish import youtube, youtube_browser
    from clipwatcher.queue import telegram_bot

    dockerfile = (config.ROOT / "deploy" / "docker" / "Dockerfile").read_text()
    assert "CLIPWATCHER_YOUTUBE_MODE=api" in dockerfile and "CLIPWATCHER_STAGING=/data/staging" in dockerfile
    monkeypatch.setattr(config, "global_config", lambda: {"publish": {"youtube_mode": "browser"}})
    monkeypatch.delenv("CLIPWATCHER_YOUTUBE_MODE", raising=False)
    assert telegram_bot._publisher() is youtube_browser
    monkeypatch.setenv("CLIPWATCHER_YOUTUBE_MODE", "api")
    assert telegram_bot._publisher() is youtube


def test_install_lines_include_the_publishers():
    """Approve uploads to YouTube: an install without the publish extras can only fail at that point."""
    extras = set(
        tomllib.loads((config.ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    )
    for path, needed in (
        ("README.md", {"publish", "publish-browser"}),  # desktop: real Chrome (default) or the Data API
        ("deploy/docker/Dockerfile", {"publish"}),  # no display in a container: Data API only
        ("deploy/README.md", {"publish"}),
    ):
        lines = [ln for ln in (config.ROOT / path).read_text().splitlines() if "pip install" in ln]
        installed = {
            e for ln in lines for group in re.findall(r"\[([\w,-]+)\]", ln) for e in group.split(",")
        }
        assert needed <= installed <= extras, f"{path}: {installed}"
