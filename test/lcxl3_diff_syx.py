#!/usr/bin/env python3
"""Diff two LCXL3 .syx files — useful for finding where MIDI channel is encoded."""

from __future__ import annotations

import argparse
from pathlib import Path


def iblocks(msg: bytes) -> list[tuple[int, bytes]]:
    nlen = msg[12]
    pos = 13 + nlen
    blocks: list[tuple[int, bytes]] = []
    while pos + 11 <= len(msg) and msg[pos] == 0x49:
        blocks.append((msg[pos + 1], msg[pos : pos + 11]))
        pos += 11
    return blocks


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff LCXL3 Components .syx files")
    parser.add_argument("a", type=Path, help="First .syx (e.g. channel 1)")
    parser.add_argument("b", type=Path, help="Second .syx (e.g. channel 2)")
    args = parser.parse_args()

    def load(path: Path) -> list[bytes]:
        data = path.read_bytes()
        return [p + b"\xf7" for p in data.split(b"\xf7") if p and p[0] == 0xF0]

    msgs_a, msgs_b = load(args.a), load(args.b)
    print(f"A: {args.a} ({len(msgs_a)} messages)")
    print(f"B: {args.b} ({len(msgs_b)} messages)")
    for idx, (ma, mb) in enumerate(zip(msgs_a, msgs_b)):
        print(f"\n=== Message {idx} page=0x{ma[9]:02x} ===")
        if ma == mb:
            print("  identical")
            continue
        if len(ma) != len(mb):
            print(f"  length A={len(ma)} B={len(mb)}")
        ba, bb = iblocks(ma), iblocks(mb)
        for (sa, a), (sb, b) in zip(ba, bb):
            if a != b:
                diffs = [i for i in range(11) if a[i] != b[i]]
                print(f"  slot 0x{sa:02x} bytes {diffs}: A={a.hex(' ')}")
                print(f"  slot 0x{sb:02x} bytes {diffs}: B={b.hex(' ')}")


if __name__ == "__main__":
    main()
