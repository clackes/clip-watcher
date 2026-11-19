"""Drive the Windows server (C:\\clip-watcher) from the Mac over WinRM. No SSH, no GitHub (owner's choice).

    uv run --with pywinrm==0.5.0 scripts/remote/cw_remote.py <command> [...]

    status                       bot task, processes, disk, last log lines
    push                         send the commits the server lacks (git bundle) and reset its main to them
    test [pytest args]           run the test suite on the server (default: -q -m "not ffmpeg")
    logs [-n N] [-g REGEX]       tail today's live log (optionally only lines matching REGEX)
    restart [list|all]           restart the bot; a comma list (or all) becomes its streamers
    install                      (re)register task cw-live: runs logged on or not, starts at boot
    stop                         stop the bot and its recorders
    ps [FILE|-]                  run a PowerShell script (file or stdin) in C:\\clip-watcher
    put LOCAL REMOTE             upload a small file (configs; chunked, ~1 MB max is sensible)
    get REMOTE LOCAL             download a small file

Password: macOS Keychain item "clipwatcher-winrm" (account Administrator; "clipwatcher-winrm-<host>" for
any other host), or CW_WINRM_PASSWORD.
Host: CW_WINRM_HOST (default 207.148.28.168).
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import winrm

HOST = os.environ.get("CW_WINRM_HOST", "207.148.28.168")
USER = os.environ.get("CW_WINRM_USER", "Administrator")
# one Keychain item per server: the first box keeps the old name
KEYCHAIN = "clipwatcher-winrm" if HOST == "207.148.28.168" else f"clipwatcher-winrm-{HOST}"
REPO = Path(__file__).resolve().parents[2]
SRV = r"C:\clip-watcher"
STAGE = r"C:\cw-up"

KILL_BOT = (
    "Stop-ScheduledTask cw-live -EA SilentlyContinue; "
    "Get-CimInstance Win32_Process | "
    "? { $_.CommandLine -match 'clipwatcher\\.(live|queue)|run_live_windows' } | "
    "% { taskkill /T /F /PID $_.ProcessId 2>&1 | Out-Null }; "
    "Get-Process streamlink,ffmpeg -EA SilentlyContinue | Stop-Process -Force;"
)


def password() -> str:
    pw = os.environ.get("CW_WINRM_PASSWORD")
    if pw:
        return pw
    cmd = ["security", "find-generic-password", "-s", KEYCHAIN, "-a", USER, "-w"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"no password: add it with  security add-generic-password -s {KEYCHAIN} -a {USER} -w")
    return r.stdout.rstrip("\n")


_session: winrm.Session | None = None


def session() -> winrm.Session:
    global _session
    if _session is None:
        _session = winrm.Session(
            f"http://{HOST}:5985/wsman",
            auth=(USER, password()),
            transport="ntlm",
            read_timeout_sec=900,
            operation_timeout_sec=850,  # restarts and test runs take minutes
        )
    return _session


def ps(script: str, *, check: bool = True) -> str:
    global _session
    pre = f"$ProgressPreference='SilentlyContinue'; Set-Location {SRV} -ErrorAction SilentlyContinue; "
    for attempt in range(6):  # a long put is hundreds of calls; one network hiccup must not end it
        try:
            r = session().run_ps(pre + script)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 5:
                raise
            _session = None
            time.sleep(5 * (attempt + 1))
    out = r.std_out.decode("utf-8", "replace")
    if r.status_code and check:
        err = r.std_err.decode("utf-8", "replace")
        sys.exit(out + "\n" + err[-3000:])
    return out


def put(local: Path, remote: str) -> None:
    """Stream the file as base64 over the command's stdin: one WinRM command, not one per 2.5 KB chunk
    (that took ~15 s per chunk to a far server, 19 Nov)."""
    data = local.read_bytes()
    b = base64.b64encode(data)
    proto = session().protocol
    shell = proto.open_shell()
    try:
        script = (
            "$t = [Console]::In.ReadToEnd();"
            f" [IO.File]::WriteAllBytes('{remote}', [Convert]::FromBase64String($t));"
            f" (Get-Item '{remote}').Length"
        )
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        cmd = proto.run_command(
            shell, "powershell", ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
        )
        step = 120 * 1024  # WinRM envelopes are capped at 500 KB (MaxEnvelopeSizekb) and stdin is re-encoded
        for i in range(0, len(b), step):
            proto.send_command_input(shell, cmd, b[i : i + step], end=i + step >= len(b))
        if not b:
            proto.send_command_input(shell, cmd, b"", end=True)
        out, err, _ = proto.get_command_output(shell, cmd)
        proto.cleanup_command(shell, cmd)
    finally:
        proto.close_shell(shell)
    n = out.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    if n[0] != str(len(data)):
        tail = err.decode(errors="replace")[-800:]
        sys.exit(f"put {local}: server has {n[0]!r} bytes, expected {len(data)}\n{tail}")
    print(f"{local} -> {remote} ({n[0]} bytes)")


def get(remote: str, local: Path) -> None:
    b64 = ps(f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{remote}'))").strip()
    local.write_bytes(base64.b64decode(b64))
    print(f"{remote} -> {local} ({local.stat().st_size} bytes)")


def git(*a: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO), *a], text=True).strip()


def cmd_push(_: argparse.Namespace) -> None:
    dirty = ps("git status --porcelain --untracked-files=no").strip()
    if dirty:
        sys.exit(f"server has uncommitted changes to tracked files, not touching them:\n{dirty}")
    base = ps("git rev-parse HEAD").strip()
    head = git("rev-parse", "main")
    if base == head:
        print("server already at", head[:7])
        return
    if subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, head]).returncode:
        sys.exit(f"server main {base[:7]} is not in local main {head[:7]}: it has commits the Mac lacks")
    bundle = REPO / ".git" / "cw-push.bundle"
    git("bundle", "create", str(bundle), f"{base}..main")
    try:
        ps(f"New-Item -ItemType Directory -Force {STAGE} | Out-Null")
        put(bundle, rf"{STAGE}\inc.bundle")
    finally:
        bundle.unlink(missing_ok=True)
    fetch = rf"git fetch -q {STAGE}\inc.bundle main 2>&1 | Out-Null"
    print(ps(fetch + "; git reset -q --hard FETCH_HEAD; git log --oneline -1"))
    print("restart the bot to run the new code:  cw_remote.py restart")


def cmd_test(a: argparse.Namespace) -> None:
    args = " ".join(a.args) if a.args else '-q -m "not ffmpeg"'
    run = f" .venv\\Scripts\\python -m pytest -p no:cacheprovider {args} 2>&1 | Select-Object -Last 40"
    # Not .env: tests must not see the API keys. Only the tool path they need.
    print(ps(r"$env:CLIPWATCHER_FFMPEG='C:\ffmpeg\bin\ffmpeg.exe'; $env:PYTHONUTF8='1';" + run, check=False))


def cmd_logs(a: argparse.Namespace) -> None:
    sel = f"| Select-String -Pattern '{a.grep}' | % Line " if a.grep else ""
    print(
        ps(
            f"$f = 'work\\logs\\live-' + (Get-Date -f yyyyMMdd) + '.log'; "
            f"Get-Content $f -Encoding UTF8 {sel}| Select-Object -Last {a.n}"
        )
    )


def cmd_status(_: argparse.Namespace) -> None:
    print(
        ps(
            "'task cw-live: ' + (Get-ScheduledTask cw-live).State; "
            "'bot python: ' + ((Get-CimInstance Win32_Process "
            "| ? { $_.CommandLine -match 'clipwatcher\\.(live|queue)' } "
            "| % { ($_.CommandLine -replace '.*-m ','').Trim() } | Sort-Object -Unique) -join ', '); "
            "'recorders: streamlink ' + (Get-Process streamlink -EA SilentlyContinue).Count + ', ffmpeg ' "
            "+ (Get-Process ffmpeg -EA SilentlyContinue).Count; "
            "$d = Get-PSDrive C; "
            "'disk C: {0:N0} GB free of {1:N0}' -f ($d.Free/1GB), (($d.Free+$d.Used)/1GB); "
            "'repo: ' + (git log --oneline -1); "
            "'--- log:'; "
            "Get-Content ('work\\logs\\live-' + (Get-Date -f yyyyMMdd) + '.log') -Encoding UTF8 -Tail 8"
        )
    )


def cmd_restart(a: argparse.Namespace) -> None:
    """The task always runs run_live_windows.ps1 with no arguments; the streamer list lives in
    config\\live_streamers.txt (no list: keep it; "all": remove it = every streamer)."""
    script = KILL_BOT + " Start-Sleep 2;"
    if a.streamers == "all":
        script += r" Remove-Item config\live_streamers.txt -EA SilentlyContinue;"
    elif a.streamers:
        script += rf" Set-Content config\live_streamers.txt '{a.streamers}';"
    script += (
        " Start-ScheduledTask cw-live; Start-Sleep 25;"
        r" $l = 'config\live_streamers.txt';"
        " 'streamers: ' + $(if (Test-Path $l) { Get-Content $l } else { 'all' });"
        " 'streamlink: ' + (Get-Process streamlink -EA SilentlyContinue).Count"
    )
    print(ps(script))


def cmd_install(_: argparse.Namespace) -> None:
    """Task cw-live as a background service: "run whether the user is logged on or not" (a Remote Desktop
    sign-out killed the interactive task on 18 Nov), started at boot (Windows Update reboots). Password logon,
    not S4U: Chrome's YouTube cookies are DPAPI-encrypted and need the user's real credentials. No window:
    watch it with status/logs. Re-run after changing the Administrator password."""
    pw = password().replace("'", "''")
    print(
        ps(
            KILL_BOT + " $arg = '-ExecutionPolicy Bypass -File " + SRV + r"\scripts\run_live_windows.ps1';"
            f" $act = New-ScheduledTaskAction -Execute powershell.exe -WorkingDirectory {SRV} -Argument $arg;"
            " $trg = New-ScheduledTaskTrigger -AtStartup; $trg.Delay = 'PT1M';"
            " $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances"
            " IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries"
            " -DontStopIfGoingOnBatteries -StartWhenAvailable;"
            " Unregister-ScheduledTask cw-live -Confirm:$false -EA SilentlyContinue;"
            " Register-ScheduledTask cw-live -Action $act -Trigger $trg -Settings $set -RunLevel Highest"
            f" -User {USER} -Password '{pw}' | Out-Null;"
            " Start-ScheduledTask cw-live; Start-Sleep 30;"
            " $t = Get-ScheduledTask cw-live; 'task: ' + $t.State + ', logon ' + $t.Principal.LogonType;"
            " 'streamlink: ' + (Get-Process streamlink -EA SilentlyContinue).Count"
        )
    )


def cmd_stop(_: argparse.Namespace) -> None:
    print(
        ps(
            KILL_BOT + " 'stopped; streamlink left: ' + (Get-Process streamlink -EA SilentlyContinue).Count",
            check=False,
        )
    )


def cmd_bootstrap(a: argparse.Namespace) -> None:
    """Fresh server: tools (Python, Git, ffmpeg, Chrome, VC++), then the repo from a full bundle and the venv
    pinned to a pip freeze of the first box (--freeze). Idempotent; re-run after a timeout."""
    ps(f"New-Item -ItemType Directory -Force {STAGE} | Out-Null")
    if a.freeze:
        put(Path(a.freeze), rf"{STAGE}\freeze.txt")
    if not a.tools_only and not ps(f"Test-Path '{SRV}\\.git'").strip().startswith("True"):
        bundle = REPO / ".git" / "cw-full.bundle"
        git("bundle", "create", str(bundle), "main")
        try:
            put(bundle, rf"{STAGE}\full.bundle")
        finally:
            bundle.unlink(missing_ok=True)
    put(Path(__file__).with_name("bootstrap_windows.ps1"), rf"{STAGE}\bootstrap.ps1")
    print(ps(rf"powershell -NoProfile -ExecutionPolicy Bypass -File {STAGE}\bootstrap.ps1 2>&1", check=False))


def cmd_ps(a: argparse.Namespace) -> None:
    src = sys.stdin.read() if a.file in (None, "-") else Path(a.file).read_text()
    print(ps(src, check=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("install").set_defaults(fn=cmd_install)
    b = sub.add_parser("bootstrap")
    b.add_argument("--freeze", help="pip freeze of a working box, used as constraints")
    b.add_argument("--tools-only", action="store_true", help="skip the repo upload (Chrome etc. first)")
    b.set_defaults(fn=cmd_bootstrap)
    sub.add_parser("push").set_defaults(fn=cmd_push)
    t = sub.add_parser("test")
    t.add_argument("args", nargs="*")
    t.set_defaults(fn=cmd_test)
    lg = sub.add_parser("logs")
    lg.add_argument("-n", type=int, default=40)
    lg.add_argument("-g", "--grep")
    lg.set_defaults(fn=cmd_logs)
    r = sub.add_parser("restart")
    r.add_argument("streamers", nargs="?")
    r.set_defaults(fn=cmd_restart)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    p = sub.add_parser("ps")
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_ps)
    pu = sub.add_parser("put")
    pu.add_argument("local", type=Path)
    pu.add_argument("remote")
    pu.set_defaults(fn=lambda a: put(a.local, a.remote))
    g = sub.add_parser("get")
    g.add_argument("remote")
    g.add_argument("local", type=Path)
    g.set_defaults(fn=lambda a: get(a.remote, a.local))
    a, extra = ap.parse_known_args()
    if a.cmd == "test":
        a.args = list(a.args or []) + extra  # pytest options such as -q -k foo
    elif extra:
        ap.error("unrecognized arguments: " + " ".join(extra))
    a.fn(a)


if __name__ == "__main__":
    main()
