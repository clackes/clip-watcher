"""The Connect place of the companion app: the requests of /api/connect/... (see server.PLACES).

    GET  /api/connect                       the two keys (set or not, never the key) and the YouTube accounts
    GET  /api/connect/youtube/status        what the sign-in or check that is open has reached
    POST /api/connect/keys/<which>          {value}: save a key        .../remove   .../test
    POST /api/connect/youtube/add           a new account: Chrome opens for the sign-in
    POST /api/connect/youtube/adopt         take the single sign-in of before into the list, and check it
    POST /api/connect/youtube/cancel        close Chrome and end what is open
    POST /api/connect/youtube/<id>/signin | check | posting | remove

The app signs nobody in and uploads nothing: a person signs in, in a Chrome window that
publish/youtube_login.py opens as a process of its own, one at a time. This module starts that process from
a fixed command built from checked values, reads the status file it writes, and ends it on Cancel.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.publish import youtube_accounts, youtube_login
from clipwatcher.publish.youtube_accounts import ACCOUNT_ID, Accounts
from clipwatcher.ui import NotFound, keys

MODES = ("signin", "check")
_MAKE = threading.Lock()


def command(account_id: str, mode: str) -> list[str]:
    """The sign-in process for one account, as a fixed list (never a shell string)."""
    if not ACCOUNT_ID.fullmatch(account_id or ""):
        raise ValueError("No such account.")
    if mode not in MODES:
        raise ValueError("A sign-in or a check.")
    cmd = [sys.executable, "-m", "clipwatcher.publish.youtube_login", "--account", account_id]
    return [*cmd, "--check"] if mode == "check" else cmd


class Logins:
    """The one sign-in or check that may be open, and how the last one ended."""

    def __init__(self, accounts: Accounts | None = None, spawn: Any = None):
        self.accounts = accounts or Accounts()
        self._spawn = spawn or self._popen
        self._lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.account: str | None = None
        self.mode = ""
        self.new = False
        self.cancelled = False
        self.last: dict[str, Any] | None = None

    def _popen(self, cmd: list[str], log: Path) -> subprocess.Popen[bytes]:
        env = {**os.environ, "CLIPWATCHER_ACCOUNTS": str(self.accounts.root), "PYTHONUNBUFFERED": "1"}
        with log.open("wb") as out:
            return subprocess.Popen(  # noqa: S603 - a fixed command list built by command()
                cmd,
                cwd=config.ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                # its own process group: Cancel reaches it and what it started
                **(
                    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
                    if os.name == "nt"
                    else {"start_new_session": True}
                ),
            )

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, account_id: str, mode: str, *, new: bool = False) -> dict[str, Any]:
        cmd = command(account_id, mode)
        with self._lock:
            self.status()  # settles one that has ended
            if self.running():
                raise RuntimeError("Chrome is already open for an account. Finish there or cancel it first.")
            if self.accounts.get(account_id) is None:
                raise NotFound("No such account.")
            self.accounts.status_path(account_id).unlink(missing_ok=True)
            self.account, self.mode, self.new, self.cancelled, self.last = account_id, mode, new, False, None
            try:
                self.proc = self._spawn(cmd, self.accounts.folder(account_id) / "login.log")
            except OSError as e:
                self.proc = None
                self._settle(
                    {
                        "phase": "failed",
                        "message": f"Chrome's helper could not be started ({type(e).__name__}).",
                    }
                )
            return self.status()

    def status(self) -> dict[str, Any]:
        """What the page shows: the phase the process wrote, or how it ended."""
        with self._lock:
            if self.account is None:
                return {"phase": "idle", "running": False}
            if self.last is not None:
                return self.last
            said = youtube_accounts._read_json(self.accounts.status_path(self.account)) or {}
            phase = str(said.get("phase") or "opening")
            if self.running():
                left = (
                    max(0.0, float(said.get("deadline") or 0) - time.time()) if said.get("deadline") else None
                )
                return self._shape(phase if phase not in youtube_login.ENDS else "closing", said, True, left)
            if phase not in youtube_login.ENDS:  # the process is gone without saying how it ended
                said = (
                    {"phase": "cancelled"}
                    if self.cancelled
                    else {
                        "phase": "failed",
                        "message": "The sign-in stopped before it was finished. Try again.",
                    }
                )
            return self._settle(said)

    def _shape(
        self, phase: str, said: dict[str, Any], running: bool, left: float | None = None
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "phase": phase,
            "running": running,
            "account": self.account,
            "mode": self.mode,
            "new": self.new,
        }
        for k in ("message", "note", "channel", "channel_id"):
            if said.get(k):
                out[k] = said[k]
        if left is not None:
            out["left_s"] = round(left)
        return out

    def _settle(self, said: dict[str, Any]) -> dict[str, Any]:
        """The process has ended: keep how, and clear away an account nobody signed in to."""
        self.last = self._shape(str(said.get("phase")), said, False)
        self.proc = None
        self.accounts.sweep()
        return self.last

    def cancel(self) -> dict[str, Any]:
        """End the open sign-in: ask the process to close Chrome, then insist."""
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return self.status()
            self.cancelled = True
        if os.name == "nt":
            from clipwatcher.ui.runs import kill_tree

            kill_tree(proc, gentle_s=25.0)
            return self.status()
        # first the process alone, so it closes Chrome itself; then its whole group, hard
        for send, sig, wait in ((os.kill, signal.SIGTERM, 25.0), (os.killpg, signal.SIGKILL, 5.0)):
            try:
                send(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        return self.status()

    def forget(self) -> None:
        """The page has shown how the last one ended."""
        with self._lock:
            if not self.running():
                self.account, self.last = None, None


def logins(app: Any) -> Logins:
    with _MAKE:
        found = getattr(app, "connect_logins", None)
        if found is None:
            found = app.connect_logins = Logins()
        return found


def shutdown(app: Any) -> None:
    """The app is closing: a Chrome window it opened is closed with it."""
    found = getattr(app, "connect_logins", None)
    if found is not None and found.running():
        found.cancel()


# -- what the page asks --------------------------------------------------------------------------------------
def state(app: Any) -> dict[str, Any]:
    jobs = logins(app)
    accounts = jobs.accounts
    mode = os.environ.get("CLIPWATCHER_YOUTUBE_MODE") or config.global_config().get("publish", {}).get(
        "youtube_mode", "browser"
    )
    return {
        "keys": keys.status(),
        "youtube": {
            "accounts": [Accounts.public(r) for r in accounts.list()],
            "legacy": accounts.legacy() is not None,
            "login": jobs.status(),
            "mode": str(mode),
            # an explicit profile in the environment wins over the list (publish/youtube_browser.profile_dir)
            "pinned": bool(os.environ.get("YT_BROWSER_PROFILE")),
        },
        "dry_run": config.dry_run(),
    }


def get(app: Any, parts: list[str], query: dict[str, list[str]]) -> Any:
    if not parts:
        return state(app)
    if parts == ["youtube", "status"]:
        return logins(app).status()
    raise NotFound("Not found.")


def post(app: Any, parts: list[str], body: dict[str, Any]) -> Any:
    if len(parts) >= 2 and parts[0] == "keys":
        which = parts[1]
        if which not in keys.KEYS:
            raise NotFound("No such key.")
        if len(parts) == 2:
            return keys.save(which, body.get("value"))
        if parts[2:] == ["remove"]:
            return keys.remove(which)
        if parts[2:] == ["test"]:
            return keys.test(which)
    if len(parts) >= 2 and parts[0] == "youtube":
        return _youtube(app, parts[1:], body)
    raise NotFound("Not found.")


def _youtube(app: Any, parts: list[str], body: dict[str, Any]) -> Any:
    jobs = logins(app)
    accounts = jobs.accounts
    if parts == ["cancel"]:
        return jobs.cancel()
    if parts == ["seen"]:
        jobs.forget()
        return jobs.status()
    if parts in (["add"], ["adopt"]):
        jobs.status()
        if jobs.running():
            raise RuntimeError("Chrome is already open for an account. Finish there or cancel it first.")
        accounts.sweep()
        if parts == ["adopt"]:
            old = accounts.legacy()
            if old is None:
                raise NotFound("There is no earlier sign-in on this machine.")
            rec = accounts.create(profile=old)
            return jobs.start(rec["id"], "check", new=True)
        return jobs.start(accounts.create()["id"], "signin", new=True)
    if len(parts) == 2:
        account_id, what = parts
        if accounts.get(account_id) is None:
            raise NotFound("No such account.")
        if what in MODES:
            return jobs.start(account_id, what)
        if what == "posting":
            accounts.set_posting(account_id)
            return state(app)["youtube"]
        if what == "remove":
            if jobs.running() and jobs.account == account_id:
                raise RuntimeError("Chrome is open for this account. Cancel that first.")
            accounts.remove(account_id)
            return state(app)["youtube"]
    raise NotFound("Not found.")


def image(image_id: str) -> tuple[Path, str] | None:
    """The picture of a connected account by its id: the file and its content type, or None."""
    return Accounts().image(image_id)
