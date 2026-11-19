"""Cheap checks on the files people start the bot with (.env.example, deploy/, scripts/)."""

from clipwatcher import config


def test_env_example_has_no_inline_comments():
    """PowerShell's and systemd's loaders took 'KEY=   # hint' as a value made of the hint: the Windows start
    script handed Telegram the comment as bot token."""
    for line in (config.ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            assert name.isidentifier() and "#" not in value and value == value.strip(), line
