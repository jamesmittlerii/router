#!/usr/bin/env python3
"""Send one MIDI Control Change through a JACK MIDI output port."""

import argparse
import threading
import time

import jack


DEFAULT_TARGET_PORT = "effect_0:midi"
DEFAULT_CONTROL = 74
DEFAULT_VALUE = 18
DEFAULT_CHANNEL = 1
DEFAULT_DELAY = 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one JACK MIDI CC message.")
    parser.add_argument(
        "target_port",
        nargs="?",
        default=DEFAULT_TARGET_PORT,
        help=f"JACK MIDI destination port (default: {DEFAULT_TARGET_PORT})",
    )
    parser.add_argument(
        "--control",
        "-c",
        type=int,
        default=DEFAULT_CONTROL,
        help=f"CC number 0-127 (default: {DEFAULT_CONTROL})",
    )
    parser.add_argument(
        "--value",
        "-v",
        type=int,
        default=DEFAULT_VALUE,
        help=f"CC value 0-127 (default: {DEFAULT_VALUE})",
    )
    parser.add_argument(
        "--channel",
        "-C",
        type=int,
        default=DEFAULT_CHANNEL,
        help=f"Human MIDI channel 1-16 (default: {DEFAULT_CHANNEL})",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=0.1,
        help="Seconds to keep the JACK client alive after sending (default: 0.1)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Seconds to wait after connecting before sending (default: 0)",
    )
    args = parser.parse_args()

    control = max(0, min(127, args.control))
    value = max(0, min(127, args.value))
    channel = max(1, min(16, args.channel))

    done = threading.Event()
    ready_at = time.monotonic() + max(0.0, args.delay)
    sent = False

    client = jack.Client("jack_cc_sender", no_start_server=True)
    out_port = client.midi_outports.register("out")

    @client.set_process_callback
    def process(_frames):
        nonlocal sent
        out_port.clear_buffer()
        if sent:
            return
        if time.monotonic() < ready_at:
            return
        status = 0xB0 | (channel - 1)
        out_port.write_midi_event(0, bytes([status, control, value]))
        sent = True
        done.set()

    with client:
        target = client.get_port_by_name(args.target_port)
        if target is None:
            print(f"Could not find JACK MIDI port: {args.target_port}")
            print("Available MIDI input/destination ports:")
            for port in client.get_ports(is_midi=True, is_input=True):
                name = port if isinstance(port, str) else port.name
                print(f"  {name}")
            return 1

        client.connect(out_port, target)
        print(f"Connected {out_port.name} -> {args.target_port}")
        print(f"Sending CC {control} value={value} on channel {channel}")

        if not done.wait(2.0):
            print("Timed out before JACK process callback sent the event")
            return 1
        if args.hold > 0:
            time.sleep(args.hold)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
