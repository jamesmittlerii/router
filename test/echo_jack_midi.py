#!/usr/bin/env python3
"""Echo MIDI events from a JACK MIDI port."""

import argparse
import queue
import signal
import sys
import time

import jack
import mido


DEFAULT_SOURCE_PORT = "system_midi:capture_1"
CLIENT_NAME = "jack_midi_echo"

events: "queue.Queue[tuple[int, bytes]]" = queue.Queue(maxsize=2048)
running = True


def request_stop(_signum, _frame) -> None:
    global running
    running = False


def decode_midi(data: bytes) -> str:
    try:
        return str(mido.Message.from_bytes(data))
    except ValueError:
        return "unparseable"


def main() -> int:
    parser = argparse.ArgumentParser(description="Echo JACK MIDI events.")
    parser.add_argument(
        "source_port",
        nargs="?",
        default=DEFAULT_SOURCE_PORT,
        help=f"JACK MIDI source port to connect from (default: {DEFAULT_SOURCE_PORT})",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    client = jack.Client(CLIENT_NAME, no_start_server=True)
    in_port = client.midi_inports.register("input")

    @client.set_process_callback
    def process(_frames):
        for offset, data in in_port.incoming_midi_events():
            try:
                events.put_nowait((offset, bytes(data)))
            except queue.Full:
                pass

    with client:
        source = client.get_port_by_name(args.source_port)
        if source is None:
            print(f"Could not find JACK MIDI port: {args.source_port}", file=sys.stderr)
            print("Available MIDI output/source ports:", file=sys.stderr)
            for port in client.get_ports(is_midi=True, is_output=True):
                name = port if isinstance(port, str) else port.name
                print(f"  {name}", file=sys.stderr)
            return 1

        client.connect(source, in_port)
        print(f"Connected {args.source_port} -> {in_port.name}")
        print("Echoing MIDI events. Press Ctrl+C to stop.")

        while running:
            try:
                offset, data = events.get(timeout=0.25)
            except queue.Empty:
                continue
            print(f"offset={offset:<5} bytes={data.hex(' '):<24} {decode_midi(data)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
