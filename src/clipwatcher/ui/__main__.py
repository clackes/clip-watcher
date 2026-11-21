"""python -m clipwatcher.ui [--port 8765] [--open] [--tailscale]: start the companion app on this machine.

It binds 127.0.0.1 only. --tailscale (with `tailscale serve` in front) also trusts this machine's tailnet
name, so the page is reachable over HTTPS from your other devices; --allow-host / CLIPWATCHER_UI_HOSTS add
any other Host the page may be served under.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from clipwatcher.ui import keys

if __name__ == "__main__":
    # the project's credential file (.env), read before anything looks at the environment: what the
    # environment already sets wins, and the runs started from the app inherit the rest
    keys.load_env()

from clipwatcher import config  # noqa: E402
from clipwatcher.ui import captions, connect  # noqa: E402
from clipwatcher.ui.server import make_server  # noqa: E402


def tailnet_self() -> tuple[str, str] | None:
    """This machine's Tailscale name and IPv4 (name without the trailing dot), or None if Tailscale is not
    running. Used so the page may be reached over a ``tailscale serve`` proxy by its tailnet name."""
    import json as _json
    import shutil
    import subprocess

    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10)
        if out.returncode:
            return None
        self_ = _json.loads(out.stdout).get("Self") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    name = str(self_.get("DNSName") or "").rstrip(".")
    ips = self_.get("TailscaleIPs") or []
    ip4 = next((str(i) for i in ips if ":" not in str(i)), "")
    return (name, ip4) if name else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.ui", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--host", default="127.0.0.1", help="leave it: the app has no login and is meant for this machine"
    )
    ap.add_argument(
        "--tailscale",
        action="store_true",
        help="also trust this machine's Tailscale name, so the page works behind `tailscale serve`",
    )
    ap.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="also trust this Host header (repeatable); or set CLIPWATCHER_UI_HOSTS (comma-separated)",
    )
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)
    allow = [*a.allow_host, *(h for h in os.environ.get("CLIPWATCHER_UI_HOSTS", "").split(",") if h.strip())]
    ts_url = None
    if a.tailscale:
        found = tailnet_self()
        if found is None:
            print("--tailscale: Tailscale is not running here; serving loopback only.", file=sys.stderr)
        else:
            name, ip4 = found
            allow += [name, ip4] if ip4 else [name]
            ts_url = f"https://{name}/"
    try:
        server, app = make_server(a.host, a.port, allow=allow)
    except OSError as e:
        print(f"Port {a.port} cannot be used ({e}). Try --port {a.port + 1}.", file=sys.stderr)
        return 2
    url = f"http://{'127.0.0.1' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.port}/"  # noqa: S104
    print(f"Companion for Clip Watcher: {url}   (Ctrl-C to stop)", flush=True)
    if ts_url:
        print(f"Over Tailscale (with `tailscale serve`): {ts_url}", flush=True)
    print(
        "DRY_RUN is on: nothing is uploaded."
        if config.dry_run()
        else "DRY_RUN=0: approved clips will be uploaded.",
        flush=True,
    )
    if a.open:
        webbrowser.open(url)
    # the caption library plays at once when its previews are there: the missing ones are made now
    threading.Thread(target=captions.warm, name="caption-previews", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        connect.shutdown(app)  # a Chrome window opened for a sign-in is closed with the app
        active = app.manager.active()
        if active is not None:
            app.manager.stop(active.id)
            if active.proc is not None:
                active.proc.wait(timeout=60)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
