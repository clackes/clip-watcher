"""Move the bot to another machine: everything git does not carry, in one folder (and a zip of it).

    python scripts/move_bot.py export [--out DIR] [--no-data]    # on the old machine
    python scripts/move_bot.py import PACKAGE [--force]          # on the new one, from the new checkout

The package holds:
  full.bundle    the git history (bootstrap_windows.ps1 clones it from C:/cw-up/full.bundle;
                 elsewhere: git clone -b main full.bundle clip-watcher)
  bootstrap_windows.ps1  installs Python, Git, ffmpeg, Chrome, the VC++ runtime and the venv on Windows
  freeze.txt     the exact Python packages of this machine (pip install -c freeze.txt ...)
  private/       .env (keys and tokens: treat the package like a password), config/streamers.yaml,
                 operators.yaml, local.yaml, live_streamers.txt, models/ (the face model)
  data/          the clip queue (staging/: clips.db copied safely while the bot runs, every clip folder),
                 the whole chat of every broadcast (work/chat/) and the judge's cost ledger (work/usage/)
  MANIFEST.json  every file with its size and SHA-256; import checks them

Not in it: the YouTube sign-in. Chrome encrypts its cookies with this Windows user's key, so a copied
profile is signed out; sign in again on the new machine. Recordings, renders, logs and caches (work/ring,
work/fastlane, work/logs, work/ui) are not moved either: the bot makes them again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = [
    ".env",
    "config/streamers.yaml",
    "config/operators.yaml",
    "config/local.yaml",
    "config/live_streamers.txt",
    "config/keywords.yaml",
]
PRIVATE_DIRS = ["models"]
DATA_DIRS = ["work/chat", "work/usage"]
SKIP_SUFFIXES = (".db-wal", ".db-shm", ".lock")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _staging(root: Path) -> Path:
    s = os.environ.get("CLIPWATCHER_STAGING")
    return Path(s).expanduser() if s else root / "staging"


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.name.endswith(SKIP_SUFFIXES):
            continue
        t = dst / f.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix == ".db":
            # SQLite's own backup: a consistent copy even while the bot writes (WAL mode)
            with sqlite3.connect(f) as a, sqlite3.connect(t) as b:
                a.backup(b)
        else:
            shutil.copy2(f, t)
        n += 1
    return n


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def export(root: Path, out: Path | None = None, *, data: bool = True, zip_it: bool = True) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M")
    pkg = Path(out) if out else root / "work" / "out" / f"clipwatcher-move-{stamp}"
    if pkg.exists() and any(pkg.iterdir()):
        raise SystemExit(f"{pkg} is not empty")
    pkg.mkdir(parents=True, exist_ok=True)
    notes = []

    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        notes.append("uncommitted changes were NOT exported:\n" + dirty)
    subprocess.check_call(["git", "-C", str(root), "bundle", "create", str(pkg / "full.bundle"), "main"],
                          stderr=subprocess.DEVNULL)  # fmt: skip
    commit = _git(root, "rev-parse", "--short", "main")

    shutil.copy2(root / "scripts" / "remote" / "bootstrap_windows.ps1", pkg / "bootstrap_windows.ps1")

    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    keep = [ln for ln in freeze.splitlines() if ln and not ln.startswith("-e ") and "clipwatcher" not in ln]
    (pkg / "freeze.txt").write_text("\n".join(keep) + "\n", encoding="utf-8")

    for rel in PRIVATE:
        src = root / rel
        if src.is_file():
            t = pkg / "private" / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, t)
    for rel in PRIVATE_DIRS:
        if (root / rel).is_dir():
            _copy_tree(root / rel, pkg / "private" / rel)
    if data:
        st = _staging(root)
        if st.is_dir():
            _copy_tree(st, pkg / "data" / "staging")
        for rel in DATA_DIRS:
            if (root / rel).is_dir():
                _copy_tree(root / rel, pkg / "data" / rel)

    files = {}
    for f in sorted(pkg.rglob("*")):
        if f.is_file():
            files[f.relative_to(pkg).as_posix()] = {"size": f.stat().st_size, "sha256": _sha(f)}
    manifest = {
        "made": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "commit": commit,
        "data": data,
        "notes": notes,
        "files": files,
    }
    (pkg / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    size = sum(v["size"] for v in files.values())
    print(f"package {pkg} ({len(files)} files, {size / 1e6:.0f} MB, commit {commit})")
    for n in notes:
        print("NOTE:", n)
    if zip_it:
        z = Path(shutil.make_archive(str(pkg), "zip", root_dir=pkg))
        print(f"zip {z} ({z.stat().st_size / 1e6:.0f} MB)")
        return z
    return pkg


def _open(package: Path, tmp: Path) -> Path:
    if package.is_dir():
        return package
    with zipfile.ZipFile(package) as z:
        z.extractall(tmp)
    return tmp


def import_(root: Path, package: Path, *, force: bool = False) -> list[str]:
    tmp = root / "work" / "out" / "move-import"
    if tmp.exists():
        shutil.rmtree(tmp)
    pkg = _open(Path(package), tmp)
    manifest = json.loads((pkg / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = [rel for rel, v in manifest["files"].items() if _sha(pkg / rel) != v["sha256"]]
    if bad:
        raise SystemExit("damaged package, these files do not match MANIFEST.json:\n" + "\n".join(bad))

    plan: list[tuple[Path, Path]] = []
    for rel in manifest["files"]:
        if rel.startswith("private/"):
            plan.append((pkg / rel, root / rel.removeprefix("private/")))
        elif rel.startswith("data/staging/"):
            plan.append((pkg / rel, _staging(root) / rel.removeprefix("data/staging/")))
        elif rel.startswith("data/"):
            plan.append((pkg / rel, root / rel.removeprefix("data/")))
    there = [str(t) for _, t in plan if t.exists()]
    if there and not force:
        raise SystemExit(
            f"{len(there)} files already exist here (first: {there[0]}). Nothing copied. "
            "Run again with --force to overwrite them."
        )
    for src, t in plan:
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, t)
    print(
        f"copied {len(plan)} files from {manifest['host']} ({manifest['made']}, commit {manifest['commit']})"
    )
    if tmp.exists():
        shutil.rmtree(tmp)
    return [str(t) for _, t in plan]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", type=Path)
    e.add_argument("--no-data", action="store_true", help="keys and config only: no queue, no chat history")
    e.add_argument("--no-zip", action="store_true")
    i = sub.add_parser("import")
    i.add_argument("package", type=Path, help="the zip or folder made by export")
    i.add_argument("--force", action="store_true", help="overwrite files that already exist")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        export(ROOT, a.out, data=not a.no_data, zip_it=not a.no_zip)
    else:
        import_(ROOT, a.package, force=a.force)


if __name__ == "__main__":
    main()
