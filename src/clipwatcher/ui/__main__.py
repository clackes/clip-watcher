"""python -m clipwatcher.ui [--port 8765] [--open]: start the companion app on this machine."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from clipwatcher import config
from clipwatcher.ui import captions
from clipwatcher.ui.server import make_server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.ui", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--host", default="127.0.0.1", help="leave it: the app has no login and is meant for this machine"
    )
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)
    try:
        server, app = make_server(a.host, a.port)
    except OSError as e:
        print(f"Port {a.port} cannot be used ({e}). Try --port {a.port + 1}.", file=sys.stderr)
        return 2
    url = f"http://{'127.0.0.1' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.port}/"  # noqa: S104
    print(f"Companion for Clip Watcher: {url}   (Ctrl-C to stop)", flush=True)
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
        active = app.manager.active()
        if active is not None:
            app.manager.stop(active.id)
            if active.proc is not None:
                active.proc.wait(timeout=60)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
