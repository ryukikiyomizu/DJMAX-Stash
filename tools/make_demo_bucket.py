#!/usr/bin/env python3
"""
Build a fake bucket tree so you can try the GUI (and run the tests) offline.

    python tools/make_demo_bucket.py            # writes tools/demo_bucket
    python tools/mock_worker.py --bucket tools/demo_bucket

The files are placeholders with the right extensions and plausible sizes, not
real audio.
"""

from __future__ import annotations

import argparse
import os
import random
import struct
from pathlib import Path

# (DLC, [songs], has_chart_folder)
LAYOUT = [
    ("Arcaea", ["Grievous Lady", "Halcyon", "Axium Crisis [MX]", "Sheriruth", "Fracture Ray"], True),
    ("V Extension", ["V-Sample 01", "V-Sample 02"], True),
    ("Deemo", ["Nine Point Eight", "Myosotis"], True),
]

GEAR_FILES = ["gear_4b.png", "gear_5b.png", "gear_6b.png", "note_skin.png", "plate.dds"]
OTHER_ASSETS = ["banner.png", "preview.jpg", "description.txt"]


def ogg_bytes(size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    body = bytes(rng.getrandbits(8) for _ in range(max(0, size - 32)))
    return b"OggS\x00\x02" + b"\x00" * 20 + struct.pack("<I", size) + body


def pt_bytes(size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(size))


def mp4_bytes(size: int, seed: int) -> bytes:
    header = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    rng = random.Random(seed)
    return header + bytes(rng.getrandbits(8) for _ in range(max(0, size - len(header))))


def write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and len(str(path)) > 240:
        path = Path("\\\\?\\" + str(path.resolve()))
    path.write_bytes(data)


def build(root: Path, song_root: str = "Songs", chart_dir: str = "Chart and OGG",
          dlc_dir: str = "By_DLC", prefix: str = "djmax", big_mv: bool = True) -> int:
    """Returns the number of files written."""
    rng = random.Random(20240925)
    count = 0
    base = root / prefix / dlc_dir

    for dlc_index, (dlc, songs, has_chart) in enumerate(LAYOUT):
        dlc_path = base / dlc

        # Gears
        for name in GEAR_FILES:
            write(dlc_path / "Gears" / name, pt_bytes(rng.randint(4_000, 60_000), rng.randint(0, 10**6)))
            count += 1

        # Other Assets
        for name in OTHER_ASSETS:
            write(dlc_path / "Other Assets" / name, pt_bytes(rng.randint(1_000, 40_000), rng.randint(0, 10**6)))
            count += 1

        if not has_chart:
            continue

        for song_index, title in enumerate(songs):
            song_path = dlc_path / song_root / f"{title} [{(song_index + 1) * 111 + dlc_index}]"
            # keysound
            write(song_path / chart_dir / f"{title}.ogg",
                  ogg_bytes(rng.randint(120_000, 400_000), song_index))
            count += 1
            # charts
            for mode in ("4B", "5B", "6B", "8B"):
                write(song_path / chart_dir / f"{mode}.pt", pt_bytes(rng.randint(2_000, 20_000),
                                                                    song_index + ord(mode[0])))
                count += 1
            # an extra pattern folder to prove "download the folder" really means the folder
            write(song_path / chart_dir / "pattern" / "extra.pt", pt_bytes(3_500, song_index))
            count += 1
            # MV
            size = 2_500_000 if (big_mv and song_index == 0) else rng.randint(400_000, 900_000)
            write(song_path / "MV" / f"{title}.mp4", mp4_bytes(size, song_index + 7))
            count += 1

    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a demo bucket tree")
    parser.add_argument("--out", default="tools/demo_bucket")
    parser.add_argument("--prefix", default="djmax")
    parser.add_argument("--dlc-dir", default="By_DLC")
    parser.add_argument("--song-dir", default="Songs")
    parser.add_argument("--chart-dir", default="Chart and OGG")
    parser.add_argument("--no-big-mv", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.out)
    count = build(root, song_root=args.song_dir, chart_dir=args.chart_dir,
                  dlc_dir=args.dlc_dir, prefix=args.prefix, big_mv=not args.no_big_mv)
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    print(f"Wrote {count} files ({total / 1_048_576:.1f} MB) to {root.resolve()}")


if __name__ == "__main__":
    main()
