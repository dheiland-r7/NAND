#!/usr/bin/env python3
"""
nand_oob_strip.py

Strip OOB/spare data from raw NAND dumps and optionally save the OOB stream.

Supports two common layouts:
  - separate: [page data][page OOB]
  - adjacent: [512 data][oob_step] repeated across each page

Examples
--------
Separate layout, 2048-byte page with 64-byte OOB:
  python3 nand_oob_strip.py -i raw.bin -o main.bin --page-size 2048 --oob-size 64 --layout separate

Separate layout, also save OOB:
  python3 nand_oob_strip.py -i raw.bin -o main.bin --oob-output oob.bin --page-size 2048 --oob-size 64 --layout separate

Adjacent layout, 2048-byte page with 64-byte OOB (16 bytes per 512-byte sector):
  python3 nand_oob_strip.py -i raw.bin -o main.bin --oob-output oob.bin --page-size 2048 --oob-size 64 --layout adjacent
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass


@dataclass
class Stats:
    pages: int = 0
    empty_pages: int = 0
    data_bytes: int = 0
    oob_bytes: int = 0
    short_reads: int = 0


def human_size(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{value} bytes ({size:.2f} {units[idx]})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Strip OOB/spare data from raw NAND dumps."
    )
    parser.add_argument("-i", "--input", required=True, help="Input NAND dump file")
    parser.add_argument("-o", "--output", required=True, help="Output file for main/page data")
    parser.add_argument(
        "--oob-output",
        help="Optional output file for extracted OOB/spare data",
    )
    parser.add_argument(
        "--page-size",
        "--block-size",
        dest="page_size",
        type=int,
        required=True,
        help="Main data bytes per page (common: 2048 or 4096). "
             "'--block-size' is accepted as an alias for convenience.",
    )
    parser.add_argument(
        "--oob-size",
        type=int,
        required=True,
        help="OOB/spare bytes per page (common: 64 or 128)",
    )
    parser.add_argument(
        "--layout",
        choices=("separate", "adjacent"),
        default="separate",
        help=(
            "separate = [page][oob] per page; "
            "adjacent = [512 data][oob step] repeated across the page"
        ),
    )
    parser.add_argument(
        "--skip-empty",
        action="store_true",
        help="Do not write all-0xFF main-data pages to the output file",
    )
    parser.add_argument(
        "--stop-on-short-read",
        action="store_true",
        help="Treat a partial final page as an error instead of stopping cleanly",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.page_size <= 0 or args.oob_size <= 0:
        raise ValueError("page-size and oob-size must be positive integers")

    if args.layout == "adjacent":
        if args.page_size % 512 != 0:
            raise ValueError("adjacent layout requires page-size to be a multiple of 512")
        sectors = args.page_size // 512
        if args.oob_size % sectors != 0:
            raise ValueError(
                f"adjacent layout requires oob-size ({args.oob_size}) to divide evenly "
                f"across {sectors} x 512-byte sectors"
            )


def read_page_separate(fin, page_size: int, oob_size: int) -> tuple[bytes, bytes]:
    data = fin.read(page_size)
    if not data:
        return b"", b""

    oob = fin.read(oob_size)
    return data, oob


def read_page_adjacent(fin, page_size: int, oob_size: int) -> tuple[bytes, bytes]:
    sectors = page_size // 512
    oob_step = oob_size // sectors

    data_parts = []
    oob_parts = []

    first = fin.read(512)
    if not first:
        return b"", b""
    data_parts.append(first)

    oob_first = fin.read(oob_step)
    oob_parts.append(oob_first)

    for _ in range(1, sectors):
        data_parts.append(fin.read(512))
        oob_parts.append(fin.read(oob_step))

    return b"".join(data_parts), b"".join(oob_parts)


def main() -> int:
    args = parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    input_size = os.path.getsize(args.input)
    per_page_total = args.page_size + args.oob_size

    print("[*] NAND OOB extractor / stripper")
    print(f"[*] Input file : {args.input}")
    print(f"[*] Output file: {args.output}")
    if args.oob_output:
        print(f"[*] OOB file   : {args.oob_output}")
    print(f"[*] Layout     : {args.layout}")
    print(f"[*] Page size  : {args.page_size}")
    print(f"[*] OOB size   : {args.oob_size}")
    print(f"[*] Input size : {human_size(input_size)}")
    if input_size % per_page_total != 0:
        print(
            f"[!] Warning: input size is not an exact multiple of page+oob "
            f"({per_page_total} bytes). Final partial page may be ignored.",
            file=sys.stderr,
        )

    stats = Stats()

    reader = read_page_separate if args.layout == "separate" else read_page_adjacent

    with open(args.input, "rb") as fin, open(args.output, "wb") as fout:
        oob_handle = open(args.oob_output, "wb") if args.oob_output else None
        try:
            while True:
                data, oob = reader(fin, args.page_size, args.oob_size)
                if not data and not oob:
                    break

                if len(data) != args.page_size or len(oob) != args.oob_size:
                    stats.short_reads += 1
                    msg = (
                        f"Short read encountered on page {stats.pages}: "
                        f"data={len(data)} bytes, oob={len(oob)} bytes"
                    )
                    if args.stop_on_short_read:
                        raise IOError(msg)
                    print(f"[!] {msg}; stopping.", file=sys.stderr)
                    break

                stats.pages += 1
                stats.data_bytes += len(data)
                stats.oob_bytes += len(oob)

                if data == (b"\xFF" * len(data)):
                    stats.empty_pages += 1

                if not (args.skip_empty and data == (b"\xFF" * len(data))):
                    fout.write(data)

                if oob_handle:
                    oob_handle.write(oob)
        finally:
            if oob_handle:
                oob_handle.close()

    print("[*] Finished")
    print(f"    Pages processed : {stats.pages}")
    print(f"    Empty pages     : {stats.empty_pages}")
    print(f"    Main data       : {human_size(stats.data_bytes)}")
    print(f"    OOB data        : {human_size(stats.oob_bytes)}")
    if stats.short_reads:
        print(f"    Short reads     : {stats.short_reads}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
