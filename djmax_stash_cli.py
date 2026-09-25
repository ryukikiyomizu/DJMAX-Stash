#!/usr/bin/env python3
"""
DJMAX Stash - command line interface.

Handy for scripted/scheduled pulls, or for machines without a display.
It shares every bit of logic with the GUI (stash_core.py).

    # what's in the bucket?
    python djmax_stash_cli.py list
    python djmax_stash_cli.py list --dlc Arcaea

    # download one DLC's songs
    python djmax_stash_cli.py songs --dlc Arcaea --dlc Deemo

    # every DLC, songs + assets
    python djmax_stash_cli.py dlc --all

    # just the gears/other assets, no songs
    python djmax_stash_cli.py assets --all

    # an arbitrary folder
    python djmax_stash_cli.py folder "djmax/By_DLC/Arcaea/Gears"

    # check the connection / find the bucket layout
    python djmax_stash_cli.py doctor
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stash_core as core  # noqa: E402


def common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--api-url", dest="api_url", help="Worker URL")
    parser.add_argument("--token", help="app token")
    parser.add_argument("--root-prefix", dest="root_prefix", help='e.g. "djmax/"')
    parser.add_argument("--dlc-dir", dest="dlc_dir", help='folder that holds DLCs, e.g. "By_DLC"')
    parser.add_argument("--song-dir", dest="song_dir", help='e.g. "Songs"')
    parser.add_argument("-o", "--output", dest="output_dir", help="where to save")
    parser.add_argument("-j", "--jobs", type=int, dest="workers", help="parallel downloads")
    parser.add_argument("--config", help="explicit config.json path")
    parser.add_argument("--no-verify", action="store_true", help="skip MD5 verification")
    parser.add_argument("--overwrite", action="store_true", help="re-download existing files")
    parser.add_argument("-q", "--quiet", action="store_true", help="less output")


def build_config(args) -> core.Config:
    overrides = {k: v for k, v in vars(args).items()}
    cfg = core.load_config(getattr(args, "config", None), overrides=overrides)
    if getattr(args, "no_verify", False):
        cfg.verify_md5 = False
    return cfg.normalised()


def need_connection(cfg: core.Config) -> core.StashAPI:
    if not cfg.api_url:
        print("! No API URL configured.\n"
              "  Deploy worker/worker.js (see worker/README.md), then either:\n"
              "    python djmax_stash_cli.py doctor --api-url https://your-worker.workers.dev --token XXX\n"
              "  or launch the GUI and fill in Settings.", file=sys.stderr)
        raise SystemExit(2)
    try:
        api = core.make_api(cfg)
        api.ping()
        return api
    except core.AuthError as exc:
        print(f"! {exc}", file=sys.stderr)
        raise SystemExit(3)
    except core.StashError as exc:
        print(f"! Cannot reach {cfg.api_url}: {exc}", file=sys.stderr)
        raise SystemExit(4)


def pick_dlcs(browser: core.StashBrowser, wanted, all_flag: bool, interactive_ok=True):
    dlcs = browser.dlcs()
    if all_flag:
        if not dlcs:
            print("! No DLC folders found. Run 'doctor' to check your layout.")
            raise SystemExit(1)
        return dlcs
    if not wanted:
        if not dlcs:
            print("! No DLC folders found. Run 'doctor' to check your layout.")
            raise SystemExit(1)
        print("Available DLCs:")
        for index, dlc in enumerate(dlcs, 1):
            print(f"  {index:3d}. {dlc.name}")
        choice = input("Which DLCs? (e.g. 1,3 or 'all'): ").strip() or "all"
        if choice.lower() in ("all", "*"):
            return dlcs
        picked = []
        for piece in choice.replace(" ", ",").split(","):
            if piece.isdigit() and 1 <= int(piece) <= len(dlcs):
                picked.append(dlcs[int(piece) - 1])
        if not picked:
            print("! Nothing selected")
            raise SystemExit(1)
        return picked

    by_name = {d.name.lower(): d for d in dlcs}
    picked = []
    missing = []
    for name in wanted:
        match = by_name.get(name.lower())
        if not match:
            # allow partial matches for convenience
            partial = [d for d in dlcs if name.lower() in d.name.lower()]
            if len(partial) == 1:
                match = partial[0]
        if match:
            picked.append(match)
        else:
            missing.append(name)
    if missing:
        print(f"! Unknown DLC(s): {', '.join(missing)}")
        print("  Available: " + ", ".join(d.name for d in dlcs))
        raise SystemExit(1)
    return picked


def render_bar(stats: core.DownloadStats, width: int = 28) -> str:
    filled = int(width * stats.percent / 100.0)
    bar = "#" * filled + "." * (width - filled)
    eta = core.human_duration(stats.eta_seconds) if stats.eta_seconds else "--:--"
    return (f"\r[{bar}] {stats.percent:5.1f}%  "
            f"{core.human_bytes(stats.bytes_done)}/{core.human_bytes(stats.bytes_total)}  "
            f"{core.human_bytes(stats.speed_bps)}/s  ETA {eta}  "
            f"files {stats.files_done + stats.files_skipped}/{stats.files_total}  ")


def run_download(cfg: core.Config, items, label: str, quiet: bool = False) -> int:
    if not items:
        print("Nothing to download (no matching files).")
        return 0

    total = sum(i.size for i in items)
    print(f"\n{label}: {len(items)} files, {core.human_bytes(total)}")
    print(f"Saving to: {cfg.output_dir}")
    if not quiet:
        print("Ctrl+C to stop - a partial file is kept as .part and resumes next run.\n")

    state = {"last": ""}

    def on_event(event):
        if quiet:
            return
        kind = event["type"]
        if kind == "progress":
            stats = event["stats"]
            line = render_bar(stats)
            if line != state["last"]:
                sys.stdout.write(line)
                sys.stdout.flush()
                state["last"] = line
        elif kind == "file_error":
            sys.stdout.write("\r" + " " * 110 + "\r")
            print(f"  ! {event['item'].rel_path}: {event['error']}")

    dl = core.Downloader(core.make_api(cfg), cfg.output_dir, workers=cfg.workers,
                         verify_md5=cfg.verify_md5, resume=cfg.resume,
                         overwrite=getattr(cfg, "overwrite", False), on_event=on_event)
    try:
        result = dl.run(items)
    except KeyboardInterrupt:
        dl.cancel()
        print("\nCancelled.")
        return 130

    if not quiet:
        sys.stdout.write("\r" + " " * 110 + "\r")
    print(f"Done in {core.human_duration(result.elapsed)}: "
          f"{result.files_done} downloaded, {result.files_skipped} already present, "
          f"{result.files_failed} failed, {core.human_bytes(result.bytes_done)} transferred")
    for key, message in result.errors[:10]:
        print(f"  ! {key}: {message}")
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_list(args):
    cfg = build_config(args)
    api = need_connection(cfg)
    browser = core.StashBrowser(api)

    if args.dlc:
        for name in args.dlc:
            dlc = next((d for d in browser.dlcs() if d.name.lower() == name.lower()), None)
            if not dlc:
                print(f"! Unknown DLC {name}")
                continue
            print(f"\n{name}")
            for folder in browser.dlc_folders(dlc):
                marker = "songs" if folder.is_song_container else "assets"
                try:
                    size = browser.prefix_size(folder.prefix)
                    shown = core.human_bytes(size)
                except core.StashError:
                    shown = "?"
                print(f"  [{marker:6s}] {folder.name:<28s} {shown}")
                if folder.is_song_container and args.songs:
                    for song in browser.songs(dlc):
                        chart = "chart ok" if song.has_chart_folder else "NO CHART FOLDER"
                        print(f"             - {song.title}  ({chart})")
        return 0

    for dlc in browser.dlcs():
        try:
            size = browser.prefix_size(dlc.prefix)
            shown = core.human_bytes(size)
        except core.StashError:
            shown = "?"
        print(f"  {dlc.name:<32s} {shown}")
    return 0


def _songs_command(cfg, api, args, whole_folder: bool, label: str):
    browser = core.StashBrowser(api)
    dlcs = pick_dlcs(browser, args.dlc, args.all)
    items = []
    for dlc in dlcs:
        songs = browser.songs(dlc)
        if not songs:
            print(f"  - {dlc.name}: no songs found (is --song-dir right?)")
            continue
        selected = songs
        if args.song:
            wanted = {s.lower() for s in args.song}
            selected = [s for s in songs
                        if any(w in s.title.lower() for w in wanted)]
            if not selected:
                print(f"  - {dlc.name}: none of those titles matched "
                      f"({len(songs)} songs in this DLC)")
                continue
        no_chart = [s.title for s in selected if not s.has_chart_folder]
        if no_chart:
            print(f"  ! {dlc.name}: {len(no_chart)} song(s) have no chart folder, "
                  f"getting the whole song folder instead: "
                  f"{', '.join(no_chart[:3])}{'...' if len(no_chart) > 3 else ''}")
        print(f"  - {dlc.name}: {len(selected)} song(s)")
        items.extend(browser.plan_songs(selected, whole_song_folder=whole_folder))
    return run_download(cfg, items, label, quiet=args.quiet)


def cmd_songs(args):
    cfg = build_config(args)
    return _songs_command(cfg, need_connection(cfg), args, whole_folder=False,
                          label="Chart + keysounds")


def cmd_assets(args):
    cfg = build_config(args)
    api = need_connection(cfg)
    browser = core.StashBrowser(api)
    dlcs = pick_dlcs(browser, args.dlc, args.all)
    items = []
    for dlc in dlcs:
        if args.folder:
            for folder in browser.dlc_folders(dlc):
                if not folder.is_song_container and folder.name.lower() in {f.lower() for f in args.folder}:
                    items.extend(browser.plan_folder(folder.prefix))
        else:
            count = len(browser.plan_dlc([dlc], include_songs=False))
            print(f"  - {dlc.name}: {count} asset file(s)")
            items.extend(browser.plan_dlc([dlc], include_songs=False))
    return run_download(cfg, items, "Assets (no songs)", quiet=args.quiet)


def cmd_dlc(args):
    cfg = build_config(args)
    api = need_connection(cfg)
    browser = core.StashBrowser(api)
    dlcs = pick_dlcs(browser, args.dlc, args.all)
    items = []
    for dlc in dlcs:
        count = len(browser.plan_dlc([dlc], include_songs=not args.no_songs))
        print(f"  - {dlc.name}: {count} file(s)")
        items.extend(browser.plan_dlc([dlc], include_songs=not args.no_songs))
    return run_download(cfg, items, "DLC (assets + songs)", quiet=args.quiet)


def cmd_folder(args):
    cfg = build_config(args)
    api = need_connection(cfg)
    browser = core.StashBrowser(api)
    prefix = args.prefix.lstrip("/")
    if not prefix.endswith("/"):
        prefix += "/"
    items = browser.plan_folder(prefix)
    return run_download(cfg, items, f"Folder {prefix}", quiet=args.quiet)


def cmd_doctor(args):
    cfg = build_config(args)
    print(f"API URL     : {cfg.api_url or '(not set)'}")
    print(f"Token       : {'set (' + str(len(cfg.token)) + ' chars)' if cfg.token else '(not set)'}")
    print(f"Root prefix : {cfg.root_prefix!r}")
    print(f"DLC dir     : {cfg.dlc_dir!r}")
    print(f"Song dir    : {cfg.song_dir!r}")
    print(f"Output      : {cfg.output_dir}")
    print(f"Config file : {cfg.source_path or '(defaults only)'}")
    print()

    try:
        api = core.make_api(cfg)
    except core.StashError as exc:
        print(f"! {exc}")
        return 2

    try:
        info = api.ping()
    except core.AuthError as exc:
        print(f"! Token rejected: {exc}")
        return 3
    except core.StashError as exc:
        print(f"! Cannot reach the Worker: {exc}")
        return 4

    print("Connection  : OK")
    print(f"Worker      : v{info.get('version', '?')}   scope: {info.get('scope')}")
    print(f"Sample key  : {info.get('sample_keys') or '(none)'}")
    print()

    result = core.auto_detect_layout(core.Config(**{**cfg.__dict__, "root_prefix": ""}), api)
    for note in result["notes"]:
        print(f"  . {note}")
    if result["ok"]:
        detected = result["config"]
        print("\nDetected layout:")
        print(f"  root_prefix = {detected.root_prefix!r}")
        print(f"  dlc_dir     = {detected.dlc_dir!r}")
        print(f"  song_dir    = {detected.song_dir!r}")
        if (detected.root_prefix, detected.dlc_dir, detected.song_dir) != \
           (cfg.root_prefix, cfg.dlc_dir, cfg.song_dir):
            print("\n  (differs from your config - use --save-detected to write it out)")
            if args.save_detected and args.config:
                merged = core.Config(**{**cfg.__dict__,
                                        "root_prefix": detected.root_prefix,
                                        "dlc_dir": detected.dlc_dir,
                                        "song_dir": detected.song_dir})
                path = core.save_config(merged, args.config)
                print(f"  saved to {path}")
    else:
        print("\n! Could not work out the layout automatically.")
        print("  Check root_prefix / dlc_dir in Settings.")
        return 5

    browser = core.StashBrowser(api)
    dlcs = browser.dlcs()
    print(f"\nDLCs found: {len(dlcs)}")
    for dlc in dlcs[:20]:
        folders = [f.name for f in browser.dlc_folders(dlc)]
        songs = len(browser.songs(dlc))
        print(f"  {dlc.name:<28s} folders: {', '.join(folders) or '-':<40s} songs: {songs}")
    return 0


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="djmax_stash_cli",
        description="Download things out of a DJMAX Stash bucket.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Handy for")[1] if "Handy for" in __doc__ else None)
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {core.APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="show DLCs and their contents")
    common_args(p_list)
    p_list.add_argument("--dlc", action="append", help="inspect one DLC (repeatable)")
    p_list.add_argument("--songs", action="store_true", help="list songs inside a DLC")
    p_list.set_defaults(func=cmd_list)

    p_songs = sub.add_parser("songs", help="download Chart and OGG folders")
    common_args(p_songs)
    p_songs.add_argument("--dlc", action="append", help="DLC name (repeatable)")
    p_songs.add_argument("--all", action="store_true", help="every DLC")
    p_songs.add_argument("--song", action="append", help="only songs whose title contains this")
    p_songs.set_defaults(func=cmd_songs)

    p_assets = sub.add_parser("assets", help="download Gears / Other Assets")
    common_args(p_assets)
    p_assets.add_argument("--dlc", action="append", help="DLC name (repeatable)")
    p_assets.add_argument("--all", action="store_true", help="every DLC")
    p_assets.add_argument("--folder", action="append",
                          help="restrict to these sub-folders, e.g. --folder Gears")
    p_assets.set_defaults(func=cmd_assets)

    p_dlc = sub.add_parser("dlc", help="download whole DLC folders")
    common_args(p_dlc)
    p_dlc.add_argument("--dlc", action="append", help="DLC name (repeatable)")
    p_dlc.add_argument("--all", action="store_true", help="every DLC")
    p_dlc.add_argument("--no-songs", action="store_true", help="skip the Songs folder")
    p_dlc.set_defaults(func=cmd_dlc)

    p_folder = sub.add_parser("folder", help="download any bucket folder")
    common_args(p_folder)
    p_folder.add_argument("prefix", help="bucket path, e.g. djmax/By_DLC/Arcaea/Gears")
    p_folder.set_defaults(func=cmd_folder)

    p_doctor = sub.add_parser("doctor", help="diagnose connection and bucket layout")
    common_args(p_doctor)
    p_doctor.add_argument("--save-detected", action="store_true",
                          help="write the detected layout to --config")
    p_doctor.set_defaults(func=cmd_doctor)

    parser.add_argument("--list-dlcs", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except core.AuthError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 3
    except core.StashError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
