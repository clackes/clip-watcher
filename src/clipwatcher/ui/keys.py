"""The two secrets the bot needs, for the Connect place: Claude's API key (the judge) and Telegram's bot token
(the approval queue). They live in the project's credential file, ``.env`` in the repo root (gitignored; the
file scripts/run_live_mac.sh sources), or the file named by CLIPWATCHER_ENV_FILE.

    read_env() / load_env()         the file's variables; load_env sets the ones the environment lacks
    save(which, value) / remove()   one variable, every other line and comment kept, written whole, mode 0600
    status()                        what the page may know: set or not, and the last 4 characters
    test_claude() / test_telegram() a check that costs nothing and sends nothing

A secret never comes back out of this module: no return value, exception text or log line holds a key, a
token or an address with a token in it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# no import of clipwatcher.config up here: load_env() runs before the configuration reads the environment
ROOT = Path(__file__).resolve().parents[3]
KEYS: dict[str, dict[str, str]] = {
    "claude": {"var": "ANTHROPIC_API_KEY", "name": "Claude key"},
    "telegram": {"var": "TELEGRAM_BOT_TOKEN", "name": "Telegram bot token"},
}
CHECK_TIMEOUT_S = 15.0
_ASSIGN = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_PLAIN = re.compile(r"[A-Za-z0-9_.:/+=@%-]+")
_BOT_TOKEN = re.compile(r"\d{3,}:[A-Za-z0-9_-]{20,}")

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def env_file() -> Path:
    env = os.environ.get("CLIPWATCHER_ENV_FILE")
    return Path(env).expanduser() if env else ROOT / ".env"


# -- the file ------------------------------------------------------------------------------------------------
def _unquote(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_env(path: Path | None = None) -> dict[str, str]:
    """The variables of the credential file (the last assignment of a name wins, as in a shell)."""
    path = path or env_file()
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        m = _ASSIGN.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(2)] = _unquote(m.group(3))
    return out


def load_env(path: Path | None = None) -> list[str]:
    """Put the file's variables into this process, except the ones the environment already sets (those win).
    Returns the names taken from the file."""
    taken = []
    for name, value in read_env(path).items():
        if name not in os.environ and value != "":
            os.environ[name] = value
            taken.append(name)
    return taken


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n" if lines else "")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def set_var(name: str, value: str, path: Path | None = None) -> None:
    """Set one variable in the file: its line is replaced where it stands (or added at the end), every other
    line and comment stays as it is."""
    path = path or env_file()
    shown = value if _PLAIN.fullmatch(value) else "'" + value + "'"
    out, placed = [], False
    for line in _lines(path):
        m = _ASSIGN.match(line)
        if m and m.group(2) == name and not line.lstrip().startswith("#"):
            if not placed:
                out.append(f"{m.group(1)}{name}={shown}")
                placed = True
            continue  # a second assignment of the same name would win over the new one
        out.append(line)
    if not placed:
        out.append(f"{name}={shown}")
    _write(path, out)


def remove_var(name: str, path: Path | None = None) -> bool:
    """Take one variable out of the file. True when it was there."""
    path = path or env_file()
    lines = _lines(path)
    kept = [
        line
        for line in lines
        if not ((m := _ASSIGN.match(line)) and m.group(2) == name and not line.lstrip().startswith("#"))
    ]
    if len(kept) == len(lines):
        return False
    _write(path, kept)
    return True


# -- what the page may do ------------------------------------------------------------------------------------
def _which(which: str) -> dict[str, str]:
    if which not in KEYS:
        raise ValueError("There are two keys here: Claude's and Telegram's.")
    return KEYS[which]


def clean(value: Any) -> str:
    """A pasted secret as it is kept: spaces and quotes around it gone. Refused when it could not be one."""
    if not isinstance(value, str):
        raise ValueError("Paste the key as text.")
    v = value.strip().strip("'\"`").strip()
    if not v:
        raise ValueError("Paste the key first.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("That has a line break or a hidden character in it. Copy the key again, on its own.")
    if len(v) > 400 or not all(33 <= ord(c) <= 126 for c in v) or any(c in v for c in "'\"`\\$"):
        raise ValueError("That does not look like a key: a key is one word of letters, digits and dashes.")
    return v


def _hint(value: str) -> str:
    return value[-4:] if len(value) >= 12 else ""


def status() -> dict[str, dict[str, Any]]:
    """Per key: whether it is set and its last 4 characters, plus what the last check found."""
    checks = _checks()
    out = {}
    for which, k in KEYS.items():
        value = os.environ.get(k["var"]) or ""
        hint = _hint(value)
        last = checks.get(which)
        out[which] = {
            "set": bool(value),
            "hint": hint,
            "check": last if value and last and last.get("hint") == hint else None,
        }
    return out


def save(which: str, value: Any) -> dict[str, Any]:
    """Keep a key: in the credential file and in this process, so the next run started here has it."""
    k = _which(which)
    v = clean(value)
    if which == "telegram" and not _BOT_TOKEN.fullmatch(v):
        raise ValueError(
            "That does not look like a bot token. BotFather gives one that reads like "
            "a number, a colon, then a long word."
        )
    try:
        set_var(k["var"], v)
    except OSError as e:
        raise RuntimeError(f"The key could not be saved on this machine ({type(e).__name__}).") from None
    os.environ[k["var"]] = v
    _forget(which)
    return status()[which]


def remove(which: str) -> dict[str, Any]:
    k = _which(which)
    try:
        remove_var(k["var"])
    except OSError as e:
        raise RuntimeError(f"The key could not be removed ({type(e).__name__}).") from None
    os.environ.pop(k["var"], None)
    _forget(which)
    return status()[which]


# -- what the last check found (never the secret) ------------------------------------------------------------
def _checks_file() -> Path:
    from clipwatcher import config

    env = os.environ.get("CLIPWATCHER_UI_WORK")
    return (Path(env).expanduser() if env else config.ROOT / "work" / "ui") / "connect" / "checks.json"


def _checks() -> dict[str, Any]:
    try:
        data = json.loads(_checks_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(which: str, result: dict[str, Any]) -> None:
    data = _checks()
    data[which] = result
    path = _checks_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # the check still answered; it is only not remembered


def _forget(which: str) -> None:
    data = _checks()
    if data.pop(which, None) is not None:
        try:
            _checks_file().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def _step(name: str, state: str, detail: str = "") -> dict[str, str]:
    return {"name": name, "state": state, "detail": detail}


def _result(
    which: str, outcome: str, headline: str, steps: list[dict[str, str]], **found: Any
) -> dict[str, Any]:
    value = os.environ.get(KEYS[which]["var"]) or ""
    res = {
        "ok": outcome == "works",
        "outcome": outcome,
        "headline": headline,
        "steps": steps,
        "hint": _hint(value),
        "at": time.time(),
        **found,
    }
    if value:
        _remember(which, res)
    return res


def test(which: str) -> dict[str, Any]:
    _which(which)
    return test_claude() if which == "claude" else test_telegram()


# -- Claude --------------------------------------------------------------------------------------------------
def judge_models() -> list[tuple[str, str]]:
    """The models the judge is set to, with what each is for: (role, id). Read from the configuration only."""
    from clipwatcher import config

    j = config.global_config().get("judge", {})
    roles = [("judge_model", "The judge")]
    if j.get("mode") == "two_stage":
        roles.insert(0, ("screen_model", "The first look"))
    out, seen = [], set()
    for key, role in roles:
        mid = str(j.get(key) or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            out.append((role, mid))
    return out


def _claude_client(key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=CHECK_TIMEOUT_S, max_retries=0)


def test_claude(client: Any = None) -> dict[str, Any]:
    """Ask Claude's API for the judge's models. It bills nothing, proves the key is accepted and that the
    models the judge will call are there for this key. ``client`` is the SDK client (tests hand in a fake)."""
    key = os.environ.get(KEYS["claude"]["var"]) or ""
    saved = _step("Saved on this machine", "done", "In the project's private credentials file.")
    if not key:
        return _result("claude", "missing", "No Claude key is saved yet.", [])
    try:
        import anthropic
    except ImportError:
        return _result(
            "claude",
            "failed",
            "The key is saved, but it cannot be checked: Claude's library is not installed on this machine.",
            [saved, _step("Claude accepts the key", "failed", "pip install anthropic")],
        )
    models = judge_models()
    if not models:
        return _result(
            "claude",
            "no_model",
            "The settings name no model for the judge, so there is nothing to check the key against.",
            [saved, _step("The judge's model", "failed", "None is set.")],
        )
    try:
        client = client or _claude_client(key)
    except Exception as e:  # noqa: BLE001 - whatever the SDK says, the key stays out of the answer
        return _result(
            "claude",
            "failed",
            "The check could not be started on this machine.",
            [saved, _step("Claude accepts the key", "failed", type(e).__name__)],
        )
    accept = "Claude accepts the key"
    steps, found, accepted = [saved], [], False

    def end(outcome: str, headline: str, name: str, detail: str) -> dict[str, Any]:
        return _result("claude", outcome, headline, [*steps, _step(name, "failed", detail)], models=found)

    for role, model_id in models:
        name = accept if not accepted else f"{role}'s model"
        try:
            info = client.models.retrieve(model_id)
        except anthropic.AuthenticationError:
            return end(
                "rejected",
                "Claude did not accept this key. Copy it again from the Console, or make a new one.",
                accept,
                "The key is not known to Claude.",
            )
        except anthropic.PermissionDeniedError:
            return end(
                "rejected",
                "Claude knows this key, but it is not allowed to be used here. "
                "Whoever manages the account can allow it, or make a new key.",
                accept,
                "The key has no permission for this.",
            )
        except anthropic.NotFoundError:
            if not accepted:
                steps.append(_step(accept, "done", "The key is known to Claude."))
            return end(
                "no_model",
                f"The key works, but it has no access to the model the bot uses for {role.lower()}. "
                "Pick another model in Settings, or ask for access to this one.",
                f"{role}'s model",
                "Not available to this key.",
            )
        except anthropic.RateLimitError:
            return end("busy", "Claude is busy right now. Try again in a minute.", name, "Too many requests.")
        except anthropic.APITimeoutError:
            return end(
                "offline",
                "Claude did not answer in time. Check the internet connection and try again.",
                name,
                f"No answer within {CHECK_TIMEOUT_S:.0f} seconds.",
            )
        except anthropic.APIConnectionError:
            return end(
                "offline",
                "Claude could not be reached. Check the internet connection and try again.",
                name,
                "No connection.",
            )
        except anthropic.APIStatusError as e:
            code = int(getattr(e, "status_code", 0) or 0)
            if code >= 500:
                return end("busy", "Claude is busy right now. Try again in a minute.", name, "Try again.")
            return end(
                "failed",
                "Claude answered with something unexpected. Try again in a minute.",
                name,
                f"Answer {code}.",
            )
        if not accepted:
            steps.append(_step(accept, "done", "The key is known to Claude."))
            accepted = True
        shown = str(getattr(info, "display_name", "") or model_id)
        found.append({"role": role, "name": shown})
        steps.append(_step(f"{role}'s model", "done", f"{shown} is available to this key."))
    names = " and ".join(m["name"] for m in found)
    return _result(
        "claude", "works", f"Claude is connected. The judge will use {names}.", steps, models=found
    )


# -- Telegram ------------------------------------------------------------------------------------------------
class _Unreachable(Exception):
    """Telegram could not be asked. Carries nothing: the address has the token in it."""


def _telegram(token: str) -> Transport:
    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}"
        req = urllib.request.Request(  # noqa: S310 - https on Telegram's own host
            url,
            data=urllib.parse.urlencode(params).encode(),
            headers={"User-Agent": "clip-watcher"},
        )
        try:
            with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_S) as r:  # noqa: S310
                body = r.read(200_000)
        except urllib.error.HTTPError as e:  # Telegram says why in the body
            body = e.read(200_000)
        except Exception:  # noqa: BLE001 - the error's own text names the address, and the token with it
            raise _Unreachable() from None
        try:
            answer = json.loads(body)
        except ValueError:
            raise _Unreachable() from None
        return answer if isinstance(answer, dict) else {}

    return call


def team_chat() -> dict[str, Any]:
    """Where the cards go and who may press the buttons, from config/operators.yaml."""
    from clipwatcher import config

    data = config._load("operators.yaml", "operators.example.yaml")  # as queue/telegram_bot reads it
    ops = data.get("operators") or {}
    people = [
        str(v.get("label") or k) for k, v in ops.items() if isinstance(v, dict) and v.get("telegram_id")
    ]
    return {"chat_id": data.get("telegram_chat_id") or 0, "operators": people}


def test_telegram(transport: Transport | None = None) -> dict[str, Any]:
    """Ask Telegram who the bot is (getMe), then for the team's group (getChat). No message is sent.
    ``transport(method, params)`` returns Telegram's JSON answer (tests hand in a fake)."""
    token = os.environ.get(KEYS["telegram"]["var"]) or ""
    saved = _step("Saved on this machine", "done", "In the project's private credentials file.")
    if not token:
        return _result("telegram", "missing", "No bot token is saved yet.", [])
    ask = transport or _telegram(token)
    knows = "Telegram knows the bot"

    def off(name: str, steps: list[dict[str, str]], **found: Any) -> dict[str, Any]:
        return _result(
            "telegram",
            "offline",
            "Telegram could not be reached. Check the internet connection and try again.",
            [*steps, _step(name, "failed", "No connection.")],
            **found,
        )

    try:
        me = ask("getMe", {})
    except _Unreachable:
        return off(knows, [saved])
    if not me.get("ok"):
        code = int(me.get("error_code") or 0)
        if code == 429 or code >= 500:
            return _result(
                "telegram",
                "busy",
                "Telegram is busy right now. Try again in a minute.",
                [saved, _step(knows, "failed", "Try again.")],
            )
        return _result(
            "telegram",
            "rejected",
            "Telegram does not know this token. Copy it again from BotFather, or ask it for a new one.",
            [saved, _step(knows, "failed", "The token is not accepted.")],
        )
    bot = me.get("result") or {}
    username = str(bot.get("username") or "")
    found: dict[str, Any] = {"bot": {"name": str(bot.get("first_name") or username), "username": username}}
    at = f"@{username}" if username else "the bot"
    steps = [saved, _step(knows, "done", f"{found['bot']['name']} ({at}).")]
    team = team_chat()
    found["operators"] = team["operators"]
    group = "The team's group"
    if not team["chat_id"]:
        return _result(
            "telegram",
            "no_group",
            f"The bot works. It has no group to post in yet: add {at} to your team's group and put the "
            "group's id in the operators file, then check again.",
            [*steps, _step(group, "skip", "No group is set yet (config/operators.yaml, telegram_chat_id).")],
            **found,
        )
    try:
        chat = ask("getChat", {"chat_id": team["chat_id"]})
    except _Unreachable:
        return off(group, steps, **found)
    if not chat.get("ok"):
        code = int(chat.get("error_code") or 0)
        if code == 429 or code >= 500:
            return _result(
                "telegram",
                "busy",
                "Telegram is busy right now. Try again in a minute.",
                [*steps, _step(group, "failed", "Try again.")],
                **found,
            )
        return _result(
            "telegram",
            "no_group",
            f"The bot works, but it is not in your team's group. Add {at} to the group, then check again.",
            [*steps, _step(group, "failed", "The bot cannot see the group that is set.")],
            **found,
        )
    info = chat.get("result") or {}
    title = str(info.get("title") or info.get("first_name") or "your chat")
    found["group"] = {"title": title, "kind": str(info.get("type") or "")}
    steps.append(_step(group, "done", f"{title}."))
    n = len(team["operators"])
    if n:
        who = "1 person" if n == 1 else f"{n} people"
        steps.append(_step("Who may approve", "done", f"{who}: {', '.join(team['operators'])}."))
    return _result(
        "telegram", "works", f"Telegram is connected. Cards for approval go to {title}.", steps, **found
    )
