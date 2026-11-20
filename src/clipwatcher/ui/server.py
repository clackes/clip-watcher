"""The companion app's server. It listens on this machine only, serves the page and its data, starts runs
when the page asks, and streams what a run is doing.

Guards, because a page on the web can make a browser call 127.0.0.1: a request must name this server as its
Host (so another name pointed at this machine is refused), and anything that changes something (POST) must
carry the token the page was handed when it was served.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.ui import captions, data, settings
from clipwatcher.ui.profiles import Profiles
from clipwatcher.ui.runs import RunManager

STATIC = Path(__file__).parent / "static"
FONTS = config.ROOT / "assets" / "fonts"
CHUNK = 256 * 1024


class App:
    def __init__(
        self,
        manager: RunManager | None = None,
        hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
        profiles: Profiles | None = None,
    ):
        self.manager = manager or RunManager()
        self.profiles = profiles or Profiles()
        self.token = secrets.token_urlsafe(24)
        self.hosts = hosts
        self._live: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._live_lock = threading.Lock()

    def live_status(self, max_age: float = 60.0) -> list[dict[str, Any]]:
        """Who of the configured streamers is live right now, and with what. Asked of the platforms (no
        login needed), at most once a minute."""
        with self._live_lock:
            at, rows = self._live
            if time.time() - at < max_age:
                return rows

            def one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
                login, scfg = item
                platform = scfg.get("platform", "twitch")
                row: dict[str, Any] = {
                    "login": login,
                    "platform": platform,
                    "live": False,
                    "category": None,
                    "error": None,
                }
                try:
                    if platform == "kick":
                        from clipwatcher.chat import kick

                        ch = kick.channel(login)
                        row.update(live=bool(ch.stream_id), category=ch.category, started_ms=ch.started_ms)
                    else:
                        from clipwatcher.chat import twitch_replay

                        s = twitch_replay.live_session(login)
                        if s is not None:
                            row.update(live=True, category=s.category, started_ms=s.started_ms)
                except Exception as e:  # noqa: BLE001 - offline, a renamed channel: say so on that row
                    row["error"] = f"{type(e).__name__}: {e}"[:160]
                return row

            with ThreadPoolExecutor(max_workers=6) as pool:
                rows = list(pool.map(one, data.own_streamers().items()))
            self._live = (time.time(), rows)
            return rows


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClipWatcherCompanion"

        def log_message(self, *a: Any) -> None:  # the terminal stays for what matters
            return

        # -- answers -----------------------------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False, default=str).encode(),
                "application/json; charset=utf-8",
            )

        def _error(self, code: int, message: str) -> None:
            self._json({"error": message}, code)

        def _file(self, path: Path, ctype: str | None = None, cache: str | None = None) -> None:
            """A file, whole or the byte range asked for (video players seek with ranges)."""
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes=") and "," not in rng:
                a, _, b = rng[6:].partition("-")
                try:
                    if a:
                        start, end = int(a), min(int(b), size - 1) if b else size - 1
                    else:
                        start = max(0, size - int(b))
                    if start > end or start >= size:
                        raise ValueError
                    code = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cache:
                self.send_header("Cache-Control", cache)
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player moved on

        # -- guards ------------------------------------------------------------------------------------
        def _own_host(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in app.hosts

        def _may_change(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).hostname not in app.hosts:
                return False
            return secrets.compare_digest(self.headers.get("X-Companion-Token") or "", app.token)

        # -- GET ---------------------------------------------------------------------------------------
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            if not self._own_host():
                return self._error(403, "This page is only served to this machine.")
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            query = urllib.parse.parse_qs(url.query)
            try:
                self._get(parts, query)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - the page gets a sentence, the server keeps running
                self._error(500, f"{type(e).__name__}: {e}")

        def _get(self, parts: list[str], query: dict[str, list[str]]) -> None:
            if not parts:
                page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            head = parts[0]
            if head == "static" and len(parts) > 1:
                path = (STATIC / Path(*parts[1:])).resolve()
                if STATIC.resolve() in path.parents and path.is_file():
                    return self._file(path)
            elif head == "fonts" and len(parts) == 2:
                path = (FONTS / parts[1]).resolve()
                if path.parent == FONTS.resolve() and path.suffix in (".ttf", ".woff2") and path.is_file():
                    return self._file(path)
            elif head == "media" and len(parts) == 3:
                path = data.media_path(parts[1], parts[2])
                if path is not None:
                    return self._file(path, "video/mp4")
            elif head == "captions" and len(parts) == 3 and parts[1] == "clip":
                path = captions.clip_path(parts[2])  # a preview, named by what it shows
                if path is not None:
                    return self._file(path, "video/mp4", cache="private, max-age=31536000, immutable")
            elif head == "profile" and len(parts) == 3 and parts[1] == "image":
                found = app.profiles.image(parts[2])  # a picture kept from Twitch or Kick, named by its hash
                if found is not None:
                    return self._file(found[0], found[1], cache="private, max-age=31536000, immutable")
            elif head == "api":
                return self._api_get(parts[1:], query)
            self._error(404, "Not found.")

        def _api_get(self, parts: list[str], query: dict[str, list[str]]) -> None:
            m = app.manager
            if parts == ["overview"]:
                active = m.active()
                return self._json({**data.overview(), "active": active.summary() if active else None})
            if parts == ["live"]:
                return self._json(app.live_status())
            if parts == ["profiles"]:  # ?who=twitch:clix,kick:n3on
                who = [w.partition(":") for w in (query.get("who") or [""])[0].split(",") if w][:60]
                try:
                    return self._json(app.profiles.many([(a, b) for a, _, b in who]))
                except ValueError as e:
                    return self._error(400, str(e))
            if parts == ["captions"]:
                return self._json(captions.library())
            if parts == ["captions", "fields"]:  # ?style=rm:punch
                try:
                    return self._json(captions.fields((query.get("style") or [""])[0]))
                except ValueError as e:
                    return self._error(400, str(e))
            if parts == ["clips"]:
                return self._json(data.clips())
            if len(parts) == 2 and parts[0] == "clips":
                found = data.clip(parts[1])
                return self._json(found) if found else self._error(404, "No such clip.")
            if parts == ["insights"]:
                return self._json(data.insights(runs=m.history()))
            if parts == ["settings"]:
                return self._json(
                    {"groups": settings.schema(), "file": str(config.CONFIG_DIR / "local.yaml")}
                )
            if parts == ["runs"]:
                return self._json(m.history())
            if len(parts) >= 2 and parts[0] == "runs":
                run = m.get(parts[1])
                if len(parts) == 3 and parts[2] == "events" and run is not None:
                    return self._stream(run, int((query.get("after") or ["0"])[0] or 0))
                if len(parts) == 2:  # the run and everything it did, to watch it again
                    past = None if run else next((r for r in m.history(500) if r["id"] == parts[1]), None)
                    if run is None and past is None:
                        return self._error(404, "No such run.")
                    summary = run.summary() if run else past
                    return self._json({**summary, "events": m.events_of(parts[1]) or []})
            self._error(404, "Not found.")

        def _stream(self, run: Any, seq: int) -> None:
            """Server-sent events: what the run did since event ``seq``, then each thing as it happens."""
            last = self.headers.get("Last-Event-ID")
            if last and last.isdigit():
                seq = max(seq, int(last))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                events = run.after(seq, timeout=15.0)
                out = "".join(
                    f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
                    for e in events
                )
                self.wfile.write((out or ": still here\n\n").encode())
                self.wfile.flush()
                if events:
                    seq = events[-1]["seq"]
                if run.state != "running" and len(run.events) <= seq:
                    return

        # -- POST --------------------------------------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            if not self._own_host() or not self._may_change():
                return self._error(403, "Open the page from this server and try again.")
            parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if 0 < length <= 1_000_000 else {}
                if not isinstance(body, dict):
                    raise ValueError("Send an object.")
                self._post(parts, body)
            except ValueError as e:
                self._error(400, str(e))
            except RuntimeError as e:
                self._error(409, str(e))
            except Exception as e:  # noqa: BLE001
                self._error(500, f"{type(e).__name__}: {e}")

        def _post(self, parts: list[str], body: dict[str, Any]) -> None:
            m = app.manager
            if parts == ["api", "runs"]:
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("params: an object.")
                return self._json(m.start(str(body.get("kind") or ""), params).summary(), 201)
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stop":
                return self._json({"stopping": m.stop(parts[2])})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "force":
                m.force(parts[2], float(body.get("seconds") or 45))
                return self._json({"forced": True})
            if parts == ["api", "captions", "preview"]:  # made once per style and settings, then kept
                made = captions.preview(str(body.get("style") or ""), body.get("settings") or None)
                return self._json(
                    {"url": f"/captions/clip/{made['file'].name}", "warnings": made["warnings"]}
                )
            if parts == ["api", "captions", "defaults"]:  # this machine's own version of a template
                style = str(body.get("style") or "")
                over = captions.check(style, body.get("settings") or {})
                if not captions.rm.is_rm(style) or not captions.is_style(style):
                    raise ValueError("Only the animated templates have settings of their own.")
                settings.save_template(captions.rm.template_name(style), over)
                return self._json({"saved": True, **captions.fields(style)})
            if parts == ["api", "settings"]:
                values = body.get("values")
                if not isinstance(values, dict):
                    raise ValueError("values: an object of setting to value.")
                kept = settings.save(values, merge=body.get("merge") is True)
                return self._json({"saved": kept, "groups": settings.schema()})
            self._error(404, "Not found.")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # a page asks for a dozen pictures at once; the default of 5 waiting connections resets the rest
    request_queue_size = 128


def make_server(
    host: str = "127.0.0.1", port: int = 8765, app: App | None = None
) -> tuple[ThreadingHTTPServer, App]:
    hosts = (
        ("127.0.0.1", "localhost") if host in ("127.0.0.1", "localhost") else ("127.0.0.1", "localhost", host)
    )
    app = app or App(hosts=hosts)
    return _Server((host, port), make_handler(app)), app
