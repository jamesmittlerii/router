#!/usr/bin/env python3
"""
Query Launch Control XL3 fader positions via the DAW MIDI interface.

Novation docs (1-based MIDI channels):
  - Send queries to DAW In on channel 8 (CC 5-12 = faders left to right)
  - Replies arrive on DAW Out on channel 7
  - Enable feature controls first (standalone): Note 11, velocity 127, channel 16

Requires: python3-jack-client, mido, JACK running, LCXL3 connected.

Example:
  python3 test/lcxl3_query_faders.py
  python3 test/lcxl3_query_faders.py --cc-start 5 --cc-count 8
  python3 test/lcxl3_query_faders.py --daw-in system:midi_playback_7 --daw-out system:midi_capture_6
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from typing import Optional

try:
    import jack
    import mido
except ImportError:
    print("Requires python3-jack-client and mido", file=sys.stderr)
    raise SystemExit(1)

# Novation programmer's guide uses 1-based channel numbers in prose.
FEATURE_ENABLE_CHANNEL = 16  # Note 11 vel 127
QUERY_CHANNEL = 8            # send CC queries here (mido channel 7)
REPLY_CHANNEL = 7            # expect CC replies here (mido channel 6)


def find_midi_port_by_alias(client: jack.Client, needle: str) -> Optional[jack.Port]:
    needle_lower = needle.lower()
    for entry in client.get_ports():
        # get_ports() returns Port objects on some jack bindings, names on others.
        if isinstance(entry, str):
            port = client.get_port_by_name(entry)
        else:
            port = entry
        if port is None:
            continue
        for alias in port.aliases:
            if needle_lower in alias.lower():
                return port
    return None


def decode_mido(event_bytes: bytes):
    try:
        return mido.Message.from_bytes(event_bytes)
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query LCXL3 fader positions via DAW In/Out MIDI"
    )
    parser.add_argument(
        "--cc-start",
        type=int,
        default=5,
        help="CC number of leftmost fader (default 5)",
    )
    parser.add_argument(
        "--cc-count",
        type=int,
        default=6,
        help="Number of faders to query (default 6)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Seconds to wait for replies after queries",
    )
    parser.add_argument(
        "--daw-in",
        help="JACK port for LCXL3 DAW In (host playback → device). Auto-detect if omitted.",
    )
    parser.add_argument(
        "--daw-out",
        help="JACK port for LCXL3 DAW Out (device → host capture). Auto-detect if omitted.",
    )
    parser.add_argument(
        "--no-enable",
        action="store_true",
        help="Skip the feature-controls enable message",
    )
    args = parser.parse_args()

    cc_numbers = list(range(args.cc_start, args.cc_start + args.cc_count))

    event_q: queue.Queue[bytes] = queue.Queue(maxsize=512)

    client = jack.Client("LCXL3_FaderQuery", no_start_server=True)
    midi_in = client.midi_inports.register("in")
    midi_out = client.midi_outports.register("out")

    @client.set_process_callback
    def process(frames):
        for _offset, data in midi_in.incoming_midi_events():
            try:
                event_q.put_nowait(bytes(data))
            except queue.Full:
                pass

    client.activate()

    if args.daw_in:
        daw_in = client.get_port_by_name(args.daw_in)
    else:
        daw_in = find_midi_port_by_alias(client, "DAW-In")

    if args.daw_out:
        daw_out = client.get_port_by_name(args.daw_out)
    else:
        daw_out = find_midi_port_by_alias(client, "DAW-Out")

    if daw_in is None or daw_out is None:
        print("Could not find LCXL3 DAW-In and/or DAW-Out ports.", file=sys.stderr)
        print("Run: jack_lsp -A | grep -i LCXL3", file=sys.stderr)
        client.close()
        raise SystemExit(1)

    try:
        client.connect(midi_out, daw_in)
        client.connect(daw_out, midi_in)
    except jack.JackError as exc:
        print(f"JACK connect failed: {exc}", file=sys.stderr)
        client.close()
        raise SystemExit(1)

    print(f"Connected {midi_out.name} -> {daw_in.name}")
    print(f"Connected {daw_out.name} -> {midi_in.name}")
    print(f"Querying fader CCs {cc_numbers} (do not move faders during query)\n")

    def send_raw(msg_bytes: bytes, label: str) -> None:
        print(f"TX {label}: {msg_bytes.hex(' ')}")
        midi_out.clear_buffer()
        midi_out.write_midi_event(0, msg_bytes)

    if not args.no_enable:
        # 9F 0B 7F — Note 11, velocity 127, channel 16
        enable = bytes([0x90 | (FEATURE_ENABLE_CHANNEL - 1), 11, 127])
        send_raw(enable, "enable feature controls")
        time.sleep(0.15)

    for cc in cc_numbers:
        query = bytes([0xB0 | (QUERY_CHANNEL - 1), cc, 0])
        send_raw(query, f"query fader CC {cc}")
        time.sleep(0.03)

    print(f"\nWaiting up to {args.timeout}s for replies on MIDI channel {REPLY_CHANNEL}...")
    deadline = time.monotonic() + args.timeout
    replies: dict[int, int] = {}

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = event_q.get(timeout=min(0.1, remaining))
        except queue.Empty:
            if len(replies) >= len(cc_numbers):
                break
            continue

        msg = decode_mido(data)
        if msg is None:
            print(f"  RX raw: {data.hex(' ')}")
            continue

        print(f"  RX {msg!r}")
        if msg.type != "control_change":
            continue
        if msg.channel != (REPLY_CHANNEL - 1):
            continue
        if msg.control not in cc_numbers:
            continue

        replies[msg.control] = msg.value

    print("\n=== Fader values ===")
    all_ok = True
    for cc in cc_numbers:
        val = replies.get(cc)
        if val is not None:
            print(f"  CC {cc}: {val:3d}  ({100.0 * val / 127.0:5.1f}%)")
        else:
            print(f"  CC {cc}: (no reply)")
            all_ok = False

    if not all_ok:
        print(
            "\nSome faders did not reply. Try:\n"
            "  - LCXL3 in Custom Mode (e.g. Mode 3)\n"
            "  - Re-run without --no-enable\n"
            "  - Move a fader once, then re-run without moving (tests movement path)\n"
            "  - Confirm ports: jack_lsp -A | grep -i LCXL3",
            file=sys.stderr,
        )

    client.deactivate()
    client.close()
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
