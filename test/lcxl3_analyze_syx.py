#!/usr/bin/env python3
"""Parse a Launch Control XL3 Components .syx export for debugging."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_message(msg: bytes, index: int) -> None:
    if not msg or msg[0] != 0xF0:
        print(f"msg {index}: not SysEx")
        return
    page = msg[9] if len(msg) > 9 else None
    slot = msg[10] if len(msg) > 10 else None
    name = ""
    pos = 13
    if len(msg) > 12 and msg[11] == 0x20:
        nlen = msg[12]
        name = msg[13 : 13 + nlen].decode("ascii", "replace")
        pos = 13 + nlen
    print(f"\n=== Message {index}  len={len(msg)}  page=0x{page:02x}  slot=0x{slot:02x}  name={name!r} ===")
    while pos < len(msg) - 1:
        b = msg[pos]
        if b == 0x49 and pos + 11 <= len(msg):
            block = msg[pos : pos + 11]
            print(
                f"  I-block slot 0x{block[1]:02x}  "
                f"b3=0x{block[3]:02x} b8=0x{block[8]:02x}  {block.hex(' ')}"
            )
            pos += 11
        elif b in (0x67, 0x68, 0x6D, 0x40):
            slot_id = msg[pos + 1]
            j = pos + 2
            while j < len(msg) and msg[j] not in (0x49, 0x67, 0x68, 0x6D, 0x40):
                j += 1
            text = msg[pos + 2 : j].decode("ascii", "replace")
            tag = chr(b) if 32 <= b < 127 else f"0x{b:02x}"
            print(f"  label {tag} slot 0x{slot_id:02x} ({len(text)} chars): {text!r}")
            pos = j
        else:
            print(f"  ? 0x{b:02x} at offset {pos}")
            pos += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze LCXL3 .syx file from Components")
    parser.add_argument("syx", type=Path, help="Path to .syx file")
    args = parser.parse_args()
    data = args.syx.read_bytes()
    parts = data.split(b"\xF7")
    msgs = [p + b"\xF7" for p in parts if p and p[0] == 0xF0]
    print(f"File: {args.syx}  ({len(data)} bytes, {len(msgs)} messages)")
    for idx, msg in enumerate(msgs):
        parse_message(msg, idx)


if __name__ == "__main__":
    main()
