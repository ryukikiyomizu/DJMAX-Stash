#!/usr/bin/env python3
"""
Package the app so you can hand it to a friend.

    # a folder they can run on any OS (needs Python installed)
    python tools/build_release.py --api-url https://your-worker.workers.dev --token XXX

    # a single .exe they can double-click (needs: pip install pyinstaller)
    python tools/build_release.py --api-url ... --token XXX --exe

The token is written obfuscated into config.json, so a friend doesn't have to
type anything - but remember it is only obfuscation, not encryption: the real
safety net is that the token can only read one prefix and cannot delete.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stash_core as core  # noqa: E402

FILES = ["djmax_stash.py", "stash_core.py", "stash_tasks.py", "djmax_stash_cli.py"]
EXTRA = [("tests", "tests"), ("worker", "worker"), ("tools", "tools")]

RUN_SH = """#!/usr/bin/env bash
# Double-click (or ./run.sh) to start DJMAX Stash.
cd "$(dirname "$0")"
PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
exec $PY djmax_stash.py "$@"
"""

RUN_BAT = """@echo off
rem Double-click to start DJMAX Stash.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)
python djmax_stash.py %*
if errorlevel 1 pause
"""


def write_config(target_dir: Path, api_url: str, token: str, extra: dict) -> Path:
    # Leave output_dir empty unless asked: then each person's copy defaults to
    # ~/DJMAX Stash on *their* machine instead of the machine that built it.
    cfg = core.Config(api_url=api_url, token=token,
                      output_dir=extra.get("output_dir") or "",
                      root_prefix=extra.get("root_prefix", core.DEFAULT_ROOT),
                      dlc_dir=extra.get("dlc_dir", core.DEFAULT_DLC_DIR),
                      song_dir=extra.get("song_dir", core.DEFAULT_SONG_DIR)).normalised()
    path = target_dir / "config.json"
    core.save_config(cfg, str(path), obfuscate_token=True)
    return path


def build_folder(out_dir: Path, api_url: str, token: str, extra: dict) -> Path:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    for name in FILES:
        shutil.copy2(ROOT / name, out_dir / name)
    for _src, dest in EXTRA:
        source = ROOT / _src
        if source.exists():
            shutil.copytree(source, out_dir / dest,
                            ignore=shutil.ignore_patterns("__pycache__", "demo_bucket", "*.pyc"))

    config = write_config(out_dir, api_url, token, extra)

    (out_dir / "run.sh").write_text(RUN_SH)
    os.chmod(out_dir / "run.sh", os.stat(out_dir / "run.sh").st_mode | stat.S_IEXEC)
    (out_dir / "run.bat").write_text(RUN_BAT)

    readme = f"""DJMAX Stash
===========
Double-click run.bat (Windows) or run.sh (macOS/Linux).
It is already set up to talk to {api_url or '(no server configured)'} - just press Refresh.

The app needs Python 3.8+ with Tkinter (the default installer from python.org
includes it). No pip installs are required.

  Download -> Songs ................ the Chart and OGG folder of each song
  Download -> Assets ............... Gears / Other Assets (skips songs)
  Download -> DLC Assets + Songs ... the whole DLC folder

Save location is set in the right-hand panel. Files land in the same shape as
the bucket, e.g. By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/...
"""
    (out_dir / "READ-ME-FIRST.txt").write_text(readme)
    print(f"Folder build ready: {out_dir}")
    print(f"  config: {config}")
    return out_dir


def build_zip(folder: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, Path(folder.name) / path.relative_to(folder))
    print(f"Zip ready: {zip_path} ({zip_path.stat().st_size / 1024:.0f} KB)")
    return zip_path


def build_exe(api_url: str, token: str, extra: dict) -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("! PyInstaller is not installed. Run:  pip install pyinstaller")
        return 1

    staging = ROOT / "build" / "release-config"
    staging.mkdir(parents=True, exist_ok=True)
    write_config(staging, api_url, token, extra)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "DJMAX-Stash",
        "--add-data", f"{staging / 'config.json'}{os.pathsep}.",
        str(ROOT / "djmax_stash.py"),
    ]
    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode == 0:
        print(f"\nexe ready: {ROOT / 'dist' / 'DJMAX-Stash'}"
              f"{'.exe' if os.name == 'nt' else ''}")
        print("The bundled token is obfuscated in config.json - see the README.")
    return result.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Package DJMAX Stash for friends")
    parser.add_argument("--api-url", default="", help="Worker URL to bake in")
    parser.add_argument("--token", default="", help="APP_TOKEN to bake in (obfuscated)")
    parser.add_argument("--out", default="dist/DJMAX-Stash", help="folder build output")
    parser.add_argument("--zip", action="store_true", help="also write a .zip next to it")
    parser.add_argument("--exe", action="store_true", help="build a single-file executable")
    parser.add_argument("--output-dir", default="", help="default save folder for friends")
    parser.add_argument("--root-prefix", default=core.DEFAULT_ROOT)
    parser.add_argument("--dlc-dir", default=core.DEFAULT_DLC_DIR)
    parser.add_argument("--song-dir", default=core.DEFAULT_SONG_DIR)
    args = parser.parse_args(argv)

    if not args.api_url:
        print("! --api-url is required (the app is useless without it).")
        print("  Example: --api-url https://djmax-stash-api.you.workers.dev")
        return 2
    if not args.token:
        print("! --token is required. It is the APP_TOKEN you set with "
              "'wrangler secret put APP_TOKEN'.")
        return 2

    extra = {"output_dir": args.output_dir, "root_prefix": args.root_prefix,
             "dlc_dir": args.dlc_dir, "song_dir": args.song_dir}

    folder = build_folder(ROOT / args.out, args.api_url, args.token, extra)
    if args.zip:
        build_zip(folder, folder.with_suffix(".zip"))
    if args.exe:
        return build_exe(args.api_url, args.token, extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
