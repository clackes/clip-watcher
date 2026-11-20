"""The Connect place: the two keys (kept in the credential file, checked for free, never handed back) and
the sign-in process the app starts for a YouTube account. Nothing here calls out or starts Chrome."""

import http.client
import json
import os
import stat
import sys
import threading
import time
import urllib.error

import pytest

from clipwatcher.publish.youtube_accounts import Accounts
from clipwatcher.ui import connect, keys
from clipwatcher.ui.runs import RunManager
from clipwatcher.ui.server import App, make_server

CLAUDE_KEY = "sk-ant-api03-thisisamadeupkeyforthetests-Zx81"
BOT_TOKEN = "123456789:AAmadeupmadeupmadeupmadeupmadeup_q7Q"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """This machine as the tests see it: its own credential file, accounts folder and work folder."""
    monkeypatch.setenv("CLIPWATCHER_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CLIPWATCHER_ACCOUNTS", str(tmp_path / "home" / "youtube"))
    monkeypatch.setenv("CLIPWATCHER_UI_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("CLIPWATCHER_STAGING", str(tmp_path / "staging"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("YT_BROWSER_PROFILE", raising=False)
    return tmp_path


# -- the credential file -------------------------------------------------------------------------------------
def test_one_variable_is_set_and_removed_and_every_other_line_stays(home):
    env = home / ".env"
    env.write_text(
        "# Copy to .env\nDRY_RUN=1\n\n# From BotFather\nTELEGRAM_BOT_TOKEN=\nexport ANTHROPIC_API_KEY='old'\n"
        "OTHER=keep me\nANTHROPIC_API_KEY=older\n"
    )
    env.chmod(0o644)
    keys.set_var("ANTHROPIC_API_KEY", CLAUDE_KEY)
    assert env.read_text() == (
        "# Copy to .env\nDRY_RUN=1\n\n# From BotFather\nTELEGRAM_BOT_TOKEN=\n"
        f"export ANTHROPIC_API_KEY={CLAUDE_KEY}\nOTHER=keep me\n"
    )
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert keys.read_env()["ANTHROPIC_API_KEY"] == CLAUDE_KEY
    assert keys.read_env()["OTHER"] == "keep me"
    keys.set_var("NEW_ONE", "a b")  # a value a shell would split is quoted, and read back whole
    assert env.read_text().endswith("NEW_ONE='a b'\n") and keys.read_env()["NEW_ONE"] == "a b"
    assert keys.remove_var("ANTHROPIC_API_KEY") is True
    assert "ANTHROPIC" not in env.read_text() and "OTHER=keep me\n" in env.read_text()
    assert keys.remove_var("ANTHROPIC_API_KEY") is False
    assert not [p for p in home.iterdir() if p.name.startswith(".env.")]  # no half-written file left


def test_the_file_is_made_private_when_it_is_new(home):
    keys.set_var("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    assert (home / ".env").read_text() == f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n"
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600


def test_the_file_is_loaded_at_start_and_the_environment_wins(home, monkeypatch):
    (home / ".env").write_text(
        f"ANTHROPIC_API_KEY={CLAUDE_KEY}\nTELEGRAM_BOT_TOKEN=\nDRY_RUN=0\nCW_T_EXTRA='x y'\n"
    )
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("CW_T_EXTRA", raising=False)
    try:
        assert sorted(keys.load_env()) == ["ANTHROPIC_API_KEY", "CW_T_EXTRA"]
        assert os.environ["ANTHROPIC_API_KEY"] == CLAUDE_KEY and os.environ["CW_T_EXTRA"] == "x y"
        assert os.environ["DRY_RUN"] == "1" and "TELEGRAM_BOT_TOKEN" not in os.environ
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CW_T_EXTRA", None)


@pytest.mark.parametrize("pasted", [f"  {CLAUDE_KEY}\n", f'"{CLAUDE_KEY}"', f" '{CLAUDE_KEY}' "])
def test_a_pasted_key_loses_the_spaces_and_quotes_around_it(pasted):
    assert keys.clean(pasted) == CLAUDE_KEY


@pytest.mark.parametrize(
    "pasted", ["", "   ", "sk-ant\nSECOND=1", "sk-ant\x00x", "two words", "a$(b)", None, 7]
)
def test_what_cannot_be_a_key_is_refused(pasted):
    with pytest.raises(ValueError) as e:
        keys.clean(pasted)
    assert "SECOND" not in str(e.value)


def test_saving_a_key_never_hands_it_back(home):
    out = keys.save("claude", f"  {CLAUDE_KEY} ")
    assert out == {"set": True, "hint": "Zx81", "check": None}
    assert os.environ["ANTHROPIC_API_KEY"] == CLAUDE_KEY  # the next run started from the app has it
    assert CLAUDE_KEY not in json.dumps(keys.status())
    with pytest.raises(ValueError):
        keys.save("telegram", "not-a-bot-token")
    with pytest.raises(ValueError):
        keys.save("github", "x")
    assert keys.remove("claude") == {"set": False, "hint": "", "check": None}
    assert "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC" not in (home / ".env").read_text()


# -- the check of Claude's key -------------------------------------------------------------------------------
class _Models:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def retrieve(self, model_id):
        self.asked.append(model_id)
        got = self.answer(model_id) if callable(self.answer) else self.answer
        if isinstance(got, Exception):
            raise got
        return got


class _Client:
    def __init__(self, answer):
        self.models = _Models(answer)


def _status_error(cls, code):
    httpx2 = pytest.importorskip("httpx2")
    req = httpx2.Request("GET", "https://api.anthropic.com/v1/models/x")
    return cls(f"said {code}", response=httpx2.Response(code, request=req), body=None)


@pytest.fixture
def judge(home, monkeypatch):
    from clipwatcher import config

    monkeypatch.setattr(
        config,
        "global_config",
        lambda: {"judge": {"mode": "two_stage", "screen_model": "m-a", "judge_model": "m-b"}},
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)


def test_claude_check_asks_for_the_judges_models_and_nothing_else(judge):
    info = type("Info", (), {"display_name": "Model B"})()
    client = _Client(lambda mid: info)
    res = keys.test_claude(client)
    assert client.models.asked == ["m-a", "m-b"]  # the models the configuration names, no message sent
    assert res["ok"] and res["outcome"] == "works" and res["headline"] == "Claude is connected."
    assert [s["state"] for s in res["steps"]] == ["done", "done", "done", "done"]
    assert "Model B" in res["advice"] and res["hint"] == "Zx81"
    assert keys.status()["claude"]["check"]["outcome"] == "works"  # the hub shows it without asking again
    assert CLAUDE_KEY not in json.dumps(res) + json.dumps(keys.status())
    keys.save("claude", CLAUDE_KEY[:-4] + "Qq11")  # another key: the old check says nothing about it
    assert keys.status()["claude"]["check"] is None


def test_claude_check_says_what_went_wrong_in_plain_words(judge):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    req = httpx2.Request("GET", "https://api.anthropic.com/v1/models/x")
    cases = [
        (_status_error(anthropic.AuthenticationError, 401), "rejected", "did not accept"),
        (_status_error(anthropic.PermissionDeniedError, 403), "rejected", "not allowed"),
        (_status_error(anthropic.RateLimitError, 429), "busy", "busy"),
        (_status_error(anthropic.InternalServerError, 529), "busy", "busy"),
        (_status_error(anthropic.BadRequestError, 400), "failed", "unexpected"),
        (anthropic.APITimeoutError(request=req), "offline", "in time"),
        (anthropic.APIConnectionError(request=req), "offline", "could not be reached"),
    ]
    for error, outcome, words in cases:
        res = keys.test_claude(_Client(error))
        assert (res["outcome"], res["ok"]) == (outcome, False), error
        assert words in res["headline"] and res["steps"][-1]["state"] == "failed"
        assert CLAUDE_KEY not in json.dumps(res) and "said" not in json.dumps(res)  # no SDK text passed on
    # the key is fine, the judge's own model is not there for it
    info = type("Info", (), {"display_name": "Model A"})()
    gone = _status_error(anthropic.NotFoundError, 404)
    res = keys.test_claude(_Client(lambda mid: info if mid == "m-a" else gone))
    assert res["outcome"] == "no_model" and "the judge" in res["headline"]
    assert [s["state"] for s in res["steps"]] == ["done", "done", "done", "failed"]


def test_claude_check_without_a_key_or_a_model(home, monkeypatch):
    from clipwatcher import config

    assert keys.test_claude(_Client(None))["outcome"] == "missing"
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    monkeypatch.setattr(config, "global_config", lambda: {"judge": {}})
    client = _Client(None)
    assert keys.test_claude(client)["outcome"] == "no_model" and client.models.asked == []


# -- the check of Telegram's token ---------------------------------------------------------------------------
def _operators(monkeypatch, chat_id=-100123, people=2):
    from clipwatcher import config

    ops = {f"p{i}": {"telegram_id": 10 + i, "label": f"Person {i}"} for i in range(people)}
    ops["nobody"] = {"telegram_id": 0, "label": "Not set"}
    monkeypatch.setattr(config, "_load", lambda *a, **k: {"telegram_chat_id": chat_id, "operators": ops})


def test_telegram_check_names_the_bot_and_the_group_and_sends_nothing(home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    _operators(monkeypatch)
    asked = []

    def transport(method, params):
        asked.append((method, params))
        if method == "getMe":
            return {"ok": True, "result": {"first_name": "ClipVault", "username": "clipvault_bot"}}
        return {"ok": True, "result": {"title": "ClipVault team", "type": "supergroup"}}

    res = keys.test_telegram(transport)
    assert asked == [("getMe", {}), ("getChat", {"chat_id": -100123})]  # two questions, no message
    assert res["ok"] and res["bot"] == {"name": "ClipVault", "username": "clipvault_bot"}
    assert res["group"]["title"] == "ClipVault team" and res["operators"] == ["Person 0", "Person 1"]
    assert "@clipvault_bot posts the cards for approval in ClipVault team." == res["advice"]
    assert BOT_TOKEN not in json.dumps(res)


def test_telegram_check_says_what_to_do_about_the_group(home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    me = {"ok": True, "result": {"first_name": "ClipVault", "username": "clipvault_bot"}}
    _operators(monkeypatch, chat_id=0)
    asked = []
    res = keys.test_telegram(lambda m, p: asked.append(m) or me)
    assert asked == ["getMe"] and res["outcome"] == "no_group" and not res["ok"]
    assert "@clipvault_bot" in res["advice"] and res["steps"][-1]["state"] == "skip"
    _operators(monkeypatch)
    not_in = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    res = keys.test_telegram(lambda m, p: me if m == "getMe" else not_in)
    assert res["outcome"] == "no_group" and "not in your team's group" in res["headline"]
    assert "Add @clipvault_bot to the group" in res["advice"]


def test_telegram_check_never_passes_the_token_or_its_address_on(home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    _operators(monkeypatch)
    res = keys.test_telegram(lambda m, p: {"ok": False, "error_code": 401, "description": "Unauthorized"})
    assert res["outcome"] == "rejected" and "does not know this token" in res["headline"]
    busy = keys.test_telegram(
        lambda m, p: {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    )
    assert busy["outcome"] == "busy"
    seen = []

    def no_network(req, timeout=0):
        seen.append(req.full_url)
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(keys.urllib.request, "urlopen", no_network)
    res = keys.test_telegram()
    assert seen and BOT_TOKEN in seen[0]  # the real transport was the one asked
    assert (
        res["outcome"] == "offline"
        and BOT_TOKEN not in json.dumps(res)
        and "api.telegram" not in json.dumps(res)
    )
    assert keys.test_telegram(lambda m, p: {})["outcome"] == "rejected"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert keys.test_telegram(lambda m, p: {})["outcome"] == "missing"


# -- through the server --------------------------------------------------------------------------------------
@pytest.fixture
def served(home):
    app = App(RunManager(home / "runs", build=lambda kind, params: [sys.executable, "-c", "pass"]))
    server, app = make_server("127.0.0.1", 0, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], app
    connect.shutdown(app)
    server.shutdown()
    server.server_close()
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)


def _call(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"X-Companion-Token": token} if token else {}
    conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
    resp = conn.getresponse()
    raw = resp.read()
    return resp.status, raw


def test_the_page_saves_a_key_and_only_ever_sees_its_last_four(served, home):
    port, app = served
    code, raw = _call(port, "POST", "/api/connect/keys/claude", {"value": CLAUDE_KEY})
    assert code == 403 and not (home / ".env").exists()  # not without the page's token
    code, raw = _call(port, "POST", "/api/connect/keys/claude", {"value": CLAUDE_KEY}, app.token)
    assert code == 200 and json.loads(raw) == {"set": True, "hint": "Zx81", "check": None}
    code, raw = _call(port, "GET", "/api/connect")
    state = json.loads(raw)
    assert state["keys"]["claude"]["set"] and not state["keys"]["telegram"]["set"]
    assert state["youtube"]["accounts"] == [] and state["youtube"]["login"]["phase"] == "idle"
    assert CLAUDE_KEY.encode() not in raw
    assert (
        json.loads(_call(port, "GET", "/api/overview")[1])["judge_key"] is True
    )  # the rest of the page knows
    code, raw = _call(port, "POST", "/api/connect/keys/claude", {"value": "two words"}, app.token)
    assert code == 400 and "does not look like a key" in json.loads(raw)["error"]
    assert _call(port, "POST", "/api/connect/keys/other", {"value": "x"}, app.token)[0] == 404
    code, raw = _call(port, "POST", "/api/connect/keys/claude/remove", {}, app.token)
    assert code == 200 and json.loads(raw)["set"] is False
    assert json.loads(_call(port, "GET", "/api/overview")[1])["judge_key"] is False
    assert _call(port, "GET", "/api/connect/nothing")[0] == 404


# -- the sign-in process the app starts ----------------------------------------------------------------------
# a stand-in for publish/youtube_login: it says where it is in the status file and waits to be told to stop
HELPER = (
    "import json, os, signal, sys, time\n"
    "path, end = sys.argv[1], sys.argv[2]\n"
    "say = lambda phase: open(path, 'w').write(json.dumps({'phase': phase, 'deadline': time.time() + 600}))\n"
    "signal.signal(signal.SIGTERM, lambda *a: (say('cancelled'), sys.exit(1)))\n"
    "say('waiting')\n"
    "time.sleep(0.4 if end != 'wait' else 60)\n"
    "say(end)\n"
)


def _logins(home, end="wait"):
    import subprocess

    accounts = Accounts(home / "home" / "youtube")
    started = []

    def spawn(cmd, log):
        started.append(cmd)
        account_id = cmd[cmd.index("--account") + 1]
        return subprocess.Popen(
            [sys.executable, "-c", HELPER, str(accounts.status_path(account_id)), end], start_new_session=True
        )

    return connect.Logins(accounts, spawn=spawn), accounts, started


def _until(check, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("it never happened")


def test_the_sign_in_command_is_a_fixed_list_of_checked_values():
    assert connect.command("0123456789ab", "signin") == [
        sys.executable, "-m", "clipwatcher.publish.youtube_login", "--account", "0123456789ab",
    ]  # fmt: skip
    assert connect.command("0123456789ab", "check")[-1] == "--check"
    for bad in ("", "../../etc", "0123456789ab; rm -rf ~", "0123456789AB", "--help"):
        with pytest.raises(ValueError):
            connect.command(bad, "signin")
    with pytest.raises(ValueError):
        connect.command("0123456789ab", "upload")


def test_one_sign_in_at_a_time_and_cancel_ends_it_and_what_it_left(home):
    jobs, accounts, started = _logins(home)
    first = accounts.create()
    out = jobs.start(first["id"], "signin", new=True)
    assert out["running"] and out["new"] and started[0][-2:] == ["--account", first["id"]]
    assert _until(lambda: jobs.status()["phase"] == "waiting") and 0 < jobs.status()["left_s"] <= 600
    with pytest.raises(RuntimeError, match="already open"):
        jobs.start(accounts.create()["id"], "signin", new=True)
    proc = jobs.proc
    out = jobs.cancel()
    assert out == {
        "phase": "cancelled",
        "running": False,
        "account": first["id"],
        "mode": "signin",
        "new": True,
    }
    assert proc.poll() is not None  # no process left behind
    assert not accounts.folder(first["id"]).exists()  # nor the account nobody signed in to
    assert jobs.status()["phase"] == "cancelled"  # the page can still read how it ended
    jobs.forget()
    assert jobs.status() == {"phase": "idle", "running": False}


def test_a_process_that_dies_without_a_word_is_a_failed_sign_in(home):
    jobs, accounts, _ = _logins(home, end="reading")  # ends while still "reading": it never said how it ended
    rec = accounts.create()
    accounts.update(rec["id"], pending=False, name="Kept")  # an account that was there before
    jobs.start(rec["id"], "check")
    out = _until(lambda: (s := jobs.status())["running"] is False and s)
    assert out["phase"] == "failed" and "stopped before it was finished" in out["message"]
    assert accounts.get(rec["id"])["name"] == "Kept"  # a known account is not swept


def test_the_youtube_requests_of_the_page(served, home, monkeypatch):
    port, app = served
    jobs, accounts, started = _logins(home, end="closed")
    app.connect_logins = jobs
    code, raw = _call(port, "POST", "/api/connect/youtube/add", {}, app.token)
    assert code == 200 and json.loads(raw)["running"] and json.loads(raw)["mode"] == "signin"
    code, raw = _call(port, "POST", "/api/connect/youtube/add", {}, app.token)
    assert code == 409 and "already open" in json.loads(raw)["error"]
    out = _until(
        lambda: (
            (s := json.loads(_call(port, "GET", "/api/connect/youtube/status")[1]))["running"] is False and s
        )
    )
    assert out["phase"] == "closed" and accounts.list() == []
    assert json.loads(_call(port, "POST", "/api/connect/youtube/seen", {}, app.token)[1])["phase"] == "idle"
    # two accounts that are signed in: which one posts, and removing one
    a, b = accounts.create(), accounts.create()
    accounts.found(a["id"], {"channel_id": "UC" + "a" * 22, "name": "First"})
    accounts.found(b["id"], {"channel_id": "UC" + "b" * 22, "name": "Second"})
    listed = json.loads(_call(port, "GET", "/api/connect")[1])["youtube"]["accounts"]
    assert [(r["name"], r["posting"], r["state"]) for r in listed] == [
        ("First", True, "ready"),
        ("Second", False, "ready"),
    ]
    assert "profile" not in listed[0] and "picture_url" not in listed[0]
    code, raw = _call(port, "POST", f"/api/connect/youtube/{b['id']}/posting", {}, app.token)
    assert [r["posting"] for r in json.loads(raw)["accounts"]] == [False, True]
    code, raw = _call(port, "POST", f"/api/connect/youtube/{b['id']}/remove", {}, app.token)
    assert [r["name"] for r in json.loads(raw)["accounts"]] == ["First"] and json.loads(raw)["accounts"][0][
        "posting"
    ]
    assert _call(port, "POST", "/api/connect/youtube/0123456789ab/check", {}, app.token)[0] == 404
    assert _call(port, "POST", "/api/connect/youtube/adopt", {}, app.token)[0] == 404  # no sign-in of before
    assert _call(port, "GET", "/connect/image/" + "0" * 20)[0] == 404
