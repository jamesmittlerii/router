#!/usr/bin/env python3
"""
Probe Launch Control XL3 fader positions via the DAW MIDI interface.

Novation docs: analog surface query on DAW In/Out is primarily available in
DAW mode (not Custom Mode performance output). This script tries several
strategies and prints everything it hears.

Requires: python3-jack-client, mido, JACK running, LCXL3 connected.

Examples:
  python3 test/lcxl3_query_faders.py
  python3 test/lcxl3_query_faders.py --enter-daw-mode
  python3 test/lcxl3_query_faders.py --method sysex --enter-daw-mode
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

NOVATION_SYSEX_HEADER = bytes([0xF0, 0x00, 0x20, 0x29, 0x02, 0x15])

# 1-based MIDI channels in Novation prose.
CH_FEATURE_ENABLE = 16  # Note 11 vel 127, or Note 12 vel 127 for DAW mode
CH_QUERY = 8            # send CC queries (mido ch 7)
CH_REPLY_FEATURE = 7    # feature-control replies (mido ch 6)
CH_DAW_FADERS = 16      # DAW mode fader CC output (mido ch 15)


def find_midi_port_by_alias(client: jack.Client, needle: str) -> Optional[jack.Port]:
    needle_lower = needle.lower()
    for entry in client.get_ports():
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


def sysex_configure_display_trigger(fader_cc: int) -> bytes:
    """SysEx: trigger temp display for analog control (target = CC index)."""
    return NOVATION_SYSEX_HEADER + bytes([0x04, fader_cc & 0x7F, 0x7F, 0xF7])


def drain_queue(event_q: queue.Queue[bytes], seconds: float = 0.2) -> int:
    deadline = time.monotonic() + seconds
    count = 0
    while time.monotonic() < deadline:
        try:
            event_q.get(timeout=0.05)
            count += 1
        except queue.Empty:
            pass
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe LCXL3 fader positions via DAW In/Out MIDI"
    )
    parser.add_argument("--cc-start", type=int, default=5, help="Leftmost fader CC")
    parser.add_argument("--cc-count", type=int, default=6, help="Number of faders")
    parser.add_argument("--timeout", type=float, default=2.5, help="Reply wait seconds")
    parser.add_argument("--daw-in", help="JACK playback port (DAW-In). Auto-detect.")
    parser.add_argument("--daw-out", help="JACK capture port (DAW-Out). Auto-detect.")
    parser.add_argument(
        "--enter-daw-mode",
        action="store_true",
        help="Send DAW-mode enable (Note 12 vel 127 ch 16) before querying",
    )
    parser.add_argument(
        "--no-enable-features",
        action="store_true",
        help="Skip feature-controls enable (Note 11 vel 127 ch 16)",
    )
    parser.add_argument(
        "--method",
        choices=("cc-query", "sysex", "both"),
        default="both",
        help="Query strategy (default: both)",
    )
    parser.add_argument(
        "--query-value",
        type=int,
        default=-1,
        help="CC query data byte (default: try 0 then 127)",
    )
    parser.add_argument(
        "--verbose-rx",
        action="store_true",
        help="Print every incoming MIDI message",
    )
    args = parser.parse_args()

    cc_numbers = list(range(args.cc_start, args.cc_start + args.cc_count))
    query_values = (
        [args.query_value]
        if args.query_value >= 0
        else [0, 127]
    )

    event_q: queue.Queue[bytes] = queue.Queue(maxsize=1024)

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

    daw_in = (
        client.get_port_by_name(args.daw_in)
        if args.daw_in
        else find_midi_port_by_alias(client, "DAW-In")
    )
    daw_out = (
        client.get_port_by_name(args.daw_out)
        if args.daw_out
        else find_midi_port_by_alias(client, "DAW-Out")
    )

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
    print(f"Fader CCs: {cc_numbers}")
    print("Leave faders still during the query.\n")

    sent_tx: list[bytes] = []

    def send_raw(msg_bytes: bytes, label: str) -> None:
        print(f"TX {label}: {msg_bytes.hex(' ')}")
        sent_tx.append(msg_bytes)
        midi_out.clear_buffer()
        midi_out.write_midi_event(0, msg_bytes)

    if args.enter_daw_mode:
        # 9F 0C 7F — Note 12, velocity 127, channel 16
        send_raw(bytes([0x90 | (CH_FEATURE_ENABLE - 1), 12, 127]), "enter DAW mode")
        time.sleep(0.2)
        drained = drain_queue(event_q, 0.3)
        if drained:
            print(f"  (drained {drained} echo messages)")

    if not args.no_enable_features:
        # 9F 0B 7F — Note 11, velocity 127, channel 16
        send_raw(bytes([0x90 | (CH_FEATURE_ENABLE - 1), 11, 127]), "enable feature controls")
        time.sleep(0.2)
        drained = drain_queue(event_q, 0.3)
        if drained:
            print(f"  (drained {drained} echo messages)")

    if args.method in ("cc-query", "both"):
        for qval in query_values:
            for cc in cc_numbers:
                query = bytes([0xB0 | (CH_QUERY - 1), cc, qval & 0x7F])
                send_raw(query, f"CC query ch{CH_QUERY} cc={cc} val={qval}")
                time.sleep(0.03)

    if args.method in ("sysex", "both"):
        for cc in cc_numbers:
            sysex = sysex_configure_display_trigger(cc)
            send_raw(sysex, f"SysEx display trigger fader CC {cc}")
            time.sleep(0.05)

    print(f"\nListening {args.timeout}s for CC replies (any channel)...")
    deadline = time.monotonic() + args.timeout
    replies: dict[tuple[int, int], int] = {}  # (channel, cc) -> value

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = event_q.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue

        if data in sent_tx:
            continue

        msg = decode_mido(data)
        if msg is None:
            if args.verbose_rx:
                print(f"  RX raw: {data.hex(' ')}")
            continue

        if args.verbose_rx:
            print(f"  RX {msg!r}")

        if msg.type != "control_change":
            continue
        if msg.control not in cc_numbers:
            continue

        replies[(msg.channel, msg.control)] = msg.value
        ch_1based = msg.channel + 1
        print(f"  >> fader CC {msg.control} = {msg.value} (channel {ch_1based})")

    print("\n=== Best guess per fader ===")
    all_ok = True
    for cc in cc_numbers:
        # Prefer reply on feature channel, then DAW fader channel, then any.
        val = (
            replies.get((CH_REPLY_FEATURE - 1, cc))
            or replies.get((CH_DAW_FADERS - 1, cc))
            or next((v for (ch, c), v in replies.items() if c == cc), None)
        )
        if val is not None:
            print(f"  CC {cc}: {val:3d}  ({100.0 * val / 127.0:5.1f}%)")
        else:
            print(f"  CC {cc}: (no reply)")
            all_ok = False

    if not all_ok:
        print(
            "\nNo fader replies yet. Notes:\n"
            "  - Analog query may require --enter-daw-mode (Custom Mode uses main MIDI port).\n"
            "  - Echoed Note 11 on ch 16 is MIDI thru of the enable message, not a reply.\n"
            "  - Try: --enter-daw-mode --method both --verbose-rx\n"
            "  - Or move a fader on midi_capture_5 (main port) to confirm Custom Mode CC.",
            file=sys.stderr,
        )

    client.deactivate()
    client.close()
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
