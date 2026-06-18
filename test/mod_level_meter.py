#!/usr/bin/env python3
"""
Terminal level meter — taps a JACK audio port (mono).

Runs alongside mod-host; no socket connection needed. Default tap is the left
output of Calf StereoTools at effect instance 35.

Example:
  python3 scripts/mod_level_meter.py
  python3 scripts/mod_level_meter.py --port effect_35:out_l

Requires: sudo apt install python3-jack-client  (numpy not required)
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field

try:
    import jack
except ImportError:
    print(
        "python3-jack-client is required: sudo apt install python3-jack-client",
        file=sys.stderr,
    )
    raise SystemExit(1)


def block_peak_float32(buf: bytes, frames: int) -> float:
    """Peak abs sample from a JACK float32 buffer (no numpy)."""
    peak = 0.0
    mv = memoryview(buf).cast("f")
    for i in range(min(frames, len(mv))):
        v = abs(mv[i])
        if v > peak:
            peak = v
    return peak


def amp_to_db(amp: float, floor_db: float) -> float:
    if amp <= 0.0:
        return floor_db
    db = 20.0 * math.log10(amp)
    return max(db, floor_db)


def db_to_bar(db: float, min_db: float, max_db: float, width: int) -> str:
    if max_db <= min_db:
        return "?" * width
    span = max_db - min_db
    pos = (db - min_db) / span
    pos = 0.0 if pos < 0.0 else 1.0 if pos > 1.0 else pos
    filled = int(round(pos * width))
    return "#" * filled + "-" * (width - filled)


def format_db(db: float) -> str:
    if db <= -59.5:
        return "-inf"
    return f"{db:5.1f}"


@dataclass
class PeakHold:
    hold_sec: float
    peak_db: float = field(default=-math.inf)
    peak_at: float = field(default=0.0)

    def update(self, db: float, now: float) -> float:
        if db >= self.peak_db:
            self.peak_db = db
            self.peak_at = now
        elif now - self.peak_at >= self.hold_sec:
            self.peak_db = db
            self.peak_at = now
        return self.peak_db


class PeakCapture:
    """Thread-safe peak accumulator between JACK process callbacks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peak = 0.0

    def push(self, peak: float) -> None:
        with self._lock:
            if peak > self._peak:
                self._peak = peak

    def take(self) -> float:
        with self._lock:
            peak = self._peak
            self._peak = 0.0
            return peak


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Terminal JACK level meter (mono tap).")
    p.add_argument(
        "--port",
        default="",
        help='JACK port to tap (default: effect_<instance>:out_l)',
    )
    p.add_argument(
        "--instance",
        type=int,
        default=35,
        help="mod-host effect instance (used when --port is omitted)",
    )
    p.add_argument("--min-db", type=float, default=-60.0, help="meter floor in dBFS")
    p.add_argument("--max-db", type=float, default=6.0, help="meter ceiling in dBFS")
    p.add_argument("--bar-width", type=int, default=40, help="bar width in characters")
    p.add_argument(
        "--peak-hold",
        type=float,
        default=3.0,
        help="seconds to hold peak before falling to current level",
    )
    p.add_argument(
        "--rate",
        type=float,
        default=25.0,
        help="display refresh rate in Hz",
    )
    p.add_argument(
        "--label",
        default="",
        help="short label in the status line (default: port name)",
    )
    p.add_argument(
        "--client-name",
        default="level_meter",
        help="JACK client name for this meter",
    )
    p.add_argument(
        "--server",
        default="",
        help="JACK server name (or set JACK_DEFAULT_SERVER)",
    )
    return p.parse_args()


def jack_connect_help() -> None:
    print(
        "\nJACK is running but this shell cannot reach it. Try:\n"
        "  ls -la /dev/shm/jack*\n"
        "  tr '\\0' '\\n' < /proc/$(pgrep -x mod-host)/environ | grep JACK\n"
        "  env | grep JACK\n"
        "  jack_lsp -s \"$JACK_DEFAULT_SERVER\"   # if server name is set\n"
        "  systemctl status jack jackd 2>/dev/null\n",
        file=sys.stderr,
    )


def open_jack_client(name: str, server: str) -> jack.Client:
    kwargs: dict = {"no_start_server": True}
    if server:
        kwargs["servername"] = server
    try:
        return jack.Client(name, **kwargs)
    except jack.JackOpenError as exc:
        print(f"cannot open JACK client: {exc}", file=sys.stderr)
        jack_connect_help()
        raise SystemExit(1) from exc


def resolve_port(args: argparse.Namespace) -> str:
    if args.port:
        return args.port
    return f"effect_{args.instance}:out_l"


def render_line(port: str, label: str, db: float, pk: float, args: argparse.Namespace) -> str:
    bar = db_to_bar(db, args.min_db, args.max_db, args.bar_width)
    name = label or port
    return f"[{name}] {format_db(db)} dBFS [{bar}]  pk {format_db(pk)}"


class StatusLine:
    """Overwrite one terminal line (works better than bare \\r in some PTYs)."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.is_tty = self.stream.isatty()
        self._last_len = 0

    def update(self, text: str) -> None:
        if not self.is_tty:
            self.stream.write(text + "\n")
            self.stream.flush()
            return

        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        if len(text) > width:
            text = text[: max(0, width - 1)]

        # CSI: column 1, erase line, then write (more reliable than \r in VS Code/Cursor).
        self.stream.write(f"\033[1G\033[2K{text}")
        self.stream.flush()
        self._last_len = len(text)

    def hide_cursor(self) -> None:
        if self.is_tty:
            self.stream.write("\033[?25l")
            self.stream.flush()

    def restore(self) -> None:
        if self.is_tty:
            self.stream.write("\033[?25h\n")
            self.stream.flush()
        elif self._last_len:
            self.stream.write("\n")
            self.stream.flush()


def main() -> int:
    args = parse_args()
    port = resolve_port(args)
    label = args.label or port
    capture = PeakCapture()
    peak = PeakHold(args.peak_hold)
    interval = 1.0 / max(args.rate, 1.0)

    client = open_jack_client(args.client_name, args.server)
    inport = client.inports.register("in")

    @client.set_process_callback
    def process(frames: int) -> int:
        capture.push(block_peak_float32(inport.get_buffer(), frames))
        return 0

    client.activate()

    try:
        client.connect(port, inport)
    except jack.JackError as exc:
        print(f"cannot connect to {port!r}: {exc}", file=sys.stderr)
        print("check the port exists: jack_lsp | grep effect_", file=sys.stderr)
        client.deactivate()
        return 1

    status = StatusLine()
    if not status.is_tty:
        print("warning: not a TTY — meter will print one line per update", file=sys.stderr)

    status.hide_cursor()
    try:
        while True:
            t0 = time.monotonic()
            amp = capture.take()
            db = amp_to_db(amp, args.min_db)
            pk = peak.update(db, t0)
            line = render_line(port, label, db, pk, args)
            status.update(line)
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        status.restore()
        client.deactivate()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
