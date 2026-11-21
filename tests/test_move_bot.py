"""scripts/move_bot.py: what export packs, that import puts it back and refuses to overwrite."""

import importlib.util
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("move_bot", ROOT / "scripts" / "move_bot.py")
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


def _repo(tmp: Path) -> Path:
    r = tmp / "old"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "README.md").write_text("x", encoding="utf-8")
    (r / "scripts" / "remote").mkdir(parents=True)
    (r / "scripts" / "remote" / "bootstrap_windows.ps1").write_text("# setup", encoding="utf-8")
    g = ["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-q", "-m", "init"], check=True)
    (r / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    (r / "config").mkdir()
    (r / "config" / "streamers.yaml").write_text("kaicenat: {}\n", encoding="utf-8")
    (r / "models").mkdir()
    (r / "models" / "face.tflite").write_bytes(b"\0model")
    st = r / "staging" / "clip-1"
    st.mkdir(parents=True)
    (st / "short_9x16.mp4").write_bytes(b"video")
    with sqlite3.connect(r / "staging" / "clips.db") as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("create table clips (id text, state text)")
        c.execute("insert into clips values ('clip-1', 'READY')")
    (r / "work" / "chat" / "kaicenat").mkdir(parents=True)
    (r / "work" / "chat" / "kaicenat" / "1.jsonl").write_text("{}\n", encoding="utf-8")
    (r / "work" / "ring").mkdir(parents=True)
    (r / "work" / "ring" / "seg.ts").write_bytes(b"big")
    return r


def test_export_then_import(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_STAGING", raising=False)
    old = _repo(tmp_path)
    z = mb.export(old, tmp_path / "pkg", data=True)
    names = set(zipfile.ZipFile(z).namelist())
    want = {
        "full.bundle", "bootstrap_windows.ps1", "freeze.txt", "MANIFEST.json", "private/.env",
        "private/config/streamers.yaml", "private/models/face.tflite", "data/staging/clips.db",
        "data/staging/clip-1/short_9x16.mp4", "data/work/chat/kaicenat/1.jsonl",
    }  # fmt: skip
    assert want <= names
    assert not any("ring" in n or n.endswith("-wal") for n in names)

    new = tmp_path / "new"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(tmp_path / "pkg" / "full.bundle"), str(new)], check=True
    )
    mb.import_(new, z)
    assert (new / ".env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=secret\n"
    assert (new / "staging" / "clip-1" / "short_9x16.mp4").read_bytes() == b"video"
    with sqlite3.connect(new / "staging" / "clips.db") as c:
        assert c.execute("select state from clips").fetchall() == [("READY",)]

    with pytest.raises(SystemExit, match="already exist"):
        mb.import_(new, z)
    (new / ".env").write_text("changed", encoding="utf-8")
    mb.import_(new, z, force=True)
    assert "secret" in (new / ".env").read_text(encoding="utf-8")


def test_keys_only_and_damaged_package(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_STAGING", raising=False)
    old = _repo(tmp_path)
    pkg = mb.export(old, tmp_path / "pkg", data=False, zip_it=False)
    m = json.loads((pkg / "MANIFEST.json").read_text(encoding="utf-8"))
    assert not any(k.startswith("data/") for k in m["files"]) and "private/.env" in m["files"]
    (pkg / "private" / ".env").write_text("tampered", encoding="utf-8")
    with pytest.raises(SystemExit, match="damaged"):
        mb.import_(tmp_path / "elsewhere", pkg)
