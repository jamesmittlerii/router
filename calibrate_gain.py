#!/usr/bin/env python3
"""
Per-instrument gain calibration for the mod-host router.

Loads a pedalboard JSON (or uses an already-loaded session), switches each
sampler instrument in turn via mod-host bypass, injects a short test chord over
JACK MIDI, measures stereo RMS at the Gain2x2 output, and prints recommended
top-level JSON `gain` values.

Run this instead of load_single.py during calibration (not alongside it).

Example:
  python3 calibrate_gain.py jsons/plus.json
  python3 calibrate_gain.py jsons/plus.json --target -18 --instrument 16
  python3 calibrate_gain.py jsons/plus.json --no-load --no-cleanup

Requires: python3-jack-client, mido is not required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import jack
except ImportError:
    print("python3-jack-client is required: sudo apt install python3-jack-client", file=sys.stderr)
    raise SystemExit(1)

MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))

INSTRUMENT_URIS = (
    "http://sfztools.github.io/sfizz",
    "https://github.com/brummer10/Fluida.lv2",
    "http://studionumbersix.com/foo/lv2/yc20",
    "http://bristol.sourceforge.net/lv2/vox",
    "https://ho-ro.net/connie/lv2",
)
OUTPUT_GAIN_URI = "http://moddevices.com/plugins/mod-devel/Gain2x2"
OUTPUT_GAIN_PARAM = "Gain"

DEFAULT_NOTES = (60, 64, 67)  # C4 E4 G4
DEFAULT_VELOCITY = 90
DEFAULT_CHANNEL = 0  # MIDI channel 1


# ---- mod-host client ----

def send_cmd(line: str) -> str:
    data = (line.rstrip("\n") + "\n").encode("utf-8", errors="replace")
    with socket.create_connection((MOD_HOST, MOD_PORT), timeout=TIMEOUT_S) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
    resp = resp.replace(b"\x00", b"")
    return resp.decode("utf-8", errors="replace").strip()


def parse_resp(resp: str) -> Optional[int]:
    r = resp.strip().replace("\x00", "")
    if not r.startswith("resp "):
        return None
    parts = r.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def expect_nonnegative(resp: str, what: str) -> int:
    code = parse_resp(resp)
    if code is None:
        raise RuntimeError(f"{what} failed (unparseable): {resp}")
    if code < 0:
        raise RuntimeError(f"{what} failed: {resp}")
    return code


def expect_zero(resp: str, what: str) -> None:
    if parse_resp(resp) != 0:
        raise RuntimeError(f"{what} failed: {resp}")


def mod_add(uri: str, instance_id: int) -> None:
    resp = send_cmd(f'add "{uri}" {instance_id}')
    expect_nonnegative(resp, f"add {instance_id}")


def mod_remove(instance_id: int) -> None:
    send_cmd(f"remove {instance_id}")


def mod_bypass(inst: int, bypass_on: bool) -> None:
    resp = send_cmd(f"bypass {inst} {1 if bypass_on else 0}")
    expect_zero(resp, f"bypass {inst}")


def mod_param_set(instance_id: int, symbol: str, value: Any) -> None:
    resp = send_cmd(f"param_set {instance_id} {symbol} {value}")
    expect_zero(resp, f"param_set {instance_id} {symbol}")


def mod_patch_set(instance_id: int, key: str, value: str) -> None:
    resp = send_cmd(f'patch_set {instance_id} "{key}" "{value}"')
    expect_zero(resp, f"patch_set {instance_id} {key}")


def mod_connect(src: str, dst: str) -> None:
    resp = send_cmd(f'connect "{src}" "{dst}"')
    expect_zero(resp, f"connect {src} -> {dst}")


def mod_disconnect(src: str, dst: str) -> None:
    send_cmd(f'disconnect "{src}" "{dst}"')


def expand_port(port: str) -> str:
    if ":" in port:
        left, right = port.split(":", 1)
        if left.isdigit():
            return f"effect_{left}:{right}"
    return port


# ---- audio metering ----

def amp_to_db(amp: float, floor_db: float = -60.0) -> float:
    if amp <= 0.0:
        return floor_db
    return max(20.0 * math.log10(amp), floor_db)


def block_sum_sq(buf: bytes, frames: int) -> tuple[float, int]:
    mv = memoryview(buf).cast("f")
    n = min(frames, len(mv))
    total = 0.0
    for i in range(n):
        v = mv[i]
        total += v * v
    return total, n


class RmsCapture:
    """Thread-safe stereo RMS accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sum_sq = 0.0
        self._count = 0

    def reset(self) -> None:
        with self._lock:
            self._sum_sq = 0.0
            self._count = 0

    def push(self, left: bytes, right: bytes, frames: int) -> None:
        l_sq, l_n = block_sum_sq(left, frames)
        r_sq, r_n = block_sum_sq(right, frames)
        with self._lock:
            self._sum_sq += l_sq + r_sq
            self._count += l_n + r_n

    def rms(self) -> float:
        with self._lock:
            if self._count == 0:
                return 0.0
            return math.sqrt(self._sum_sq / self._count)


# ---- pedalboard parsing ----

@dataclass
class Instrument:
    instance_id: int
    symbol: str
    current_gain: float
    midi_port: str


@dataclass
class Pedalboard:
    instruments: list[Instrument]
    gain_instance: Optional[int]
    plugins: dict[str, Any]
    connections: list[dict[str, str]]


def get_plugin_gain(plugin: dict[str, Any], fallback: float = 0.0) -> float:
    raw = plugin.get("gain", fallback)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def parse_midi_targets(connections: list[dict[str, str]]) -> dict[int, str]:
    """Map instrument instance id -> mod-host MIDI input port from pedalboard wiring."""
    targets: dict[int, str] = {}
    for conn in connections:
        if conn.get("from") not in ("system:midi_capture_1", "@alias:SL-CTRL"):
            continue
        to = conn.get("to", "")
        if ":" not in to:
            continue
        inst_s, _ = to.split(":", 1)
        if not inst_s.isdigit():
            continue
        targets[int(inst_s)] = expand_port(to)
    return targets


def parse_pedalboard(pb: dict[str, Any]) -> Pedalboard:
    plugins: dict[str, Any] = pb.get("plugins", {})
    connections: list[dict[str, str]] = pb.get("connections", [])
    gain_instance: Optional[int] = None
    instruments: list[Instrument] = []
    midi_targets = parse_midi_targets(connections)

    for sid, plugin in plugins.items():
        if not isinstance(plugin, dict):
            continue
        if plugin.get("uri") == OUTPUT_GAIN_URI:
            gain_instance = int(sid)

    for inst in sorted(midi_targets):
        plugin = plugins.get(str(inst))
        if not isinstance(plugin, dict):
            continue
        if plugin.get("uri") not in INSTRUMENT_URIS:
            continue
        instruments.append(
            Instrument(
                instance_id=inst,
                symbol=str(plugin.get("symbol", inst)),
                current_gain=get_plugin_gain(plugin),
                midi_port=midi_targets[inst],
            )
        )

    return Pedalboard(
        instruments=instruments,
        gain_instance=gain_instance,
        plugins=plugins,
        connections=connections,
    )


def load_pedalboard(board: Pedalboard) -> tuple[list[int], list[tuple[str, str]]]:
    loaded_ids: list[int] = []
    active_connections: list[tuple[str, str]] = []

    print("== Loading pedalboard ==")
    for sid in sorted(board.plugins.keys(), key=lambda x: int(x)):
        plugin = board.plugins[sid]
        inst = int(sid)
        uri = plugin["uri"]
        print(f"  add {inst} {uri}")
        mod_add(uri, inst)
        loaded_ids.append(inst)

    print("== Applying state and controls ==")
    for sid in sorted(board.plugins.keys(), key=lambda x: int(x)):
        plugin = board.plugins[sid]
        inst = int(sid)

        for key, val in (plugin.get("state") or {}).items():
            mod_patch_set(inst, key, str(val))

        for symbol, val in (plugin.get("controls") or {}).items():
            mod_param_set(inst, symbol, val)

        if "bypass" in plugin:
            mod_bypass(inst, bool(plugin["bypass"]))

    time.sleep(0.2)

    print("== Connecting ports ==")
    for conn in board.connections:
        src = expand_port(conn["from"])
        dst = expand_port(conn["to"])
        mod_connect(src, dst)
        active_connections.append((src, dst))

    return loaded_ids, active_connections


def cleanup_session(loaded_ids: list[int], active_connections: list[tuple[str, str]]) -> None:
    print("== Cleaning up pedalboard ==")
    for src, dst in reversed(active_connections):
        try:
            mod_disconnect(src, dst)
        except Exception as exc:
            print(f"  disconnect failed {src} -> {dst}: {exc}", file=sys.stderr)
    for inst in reversed(loaded_ids):
        try:
            mod_remove(inst)
        except Exception as exc:
            print(f"  remove failed {inst}: {exc}", file=sys.stderr)


# ---- MIDI helpers ----

def midi_note_on(note: int, velocity: int, channel: int) -> bytes:
    return bytes([0x90 | (channel & 0x0F), note & 0x7F, velocity & 0x7F])


def midi_note_off(note: int, channel: int) -> bytes:
    return bytes([0x80 | (channel & 0x0F), note & 0x7F, 0])


# ---- calibration ----

class Calibrator:
    def __init__(
        self,
        board: Pedalboard,
        args: argparse.Namespace,
    ) -> None:
        if board.gain_instance is None:
            raise RuntimeError(f"No {OUTPUT_GAIN_URI!r} plugin found in pedalboard JSON")

        self.board = board
        self.args = args
        self.gain_instance = board.gain_instance
        self.meter = RmsCapture()
        self.send_q: queue.Queue[bytes] = queue.Queue(maxsize=128)

        self.client = jack.Client("gain_calibrator", no_start_server=True)
        self.midi_out = self.client.midi_outports.register("midi_out")
        self.audio_l = self.client.inports.register("gain_l")
        self.audio_r = self.client.inports.register("gain_r")

        @self.client.set_process_callback
        def process(frames: int) -> int:
            self.meter.push(
                self.audio_l.get_buffer(),
                self.audio_r.get_buffer(),
                frames,
            )
            self.midi_out.clear_buffer()
            while True:
                try:
                    msg = self.send_q.get_nowait()
                except queue.Empty:
                    break
                self.midi_out.write_midi_event(0, msg)
            return 0

    def _gain_ports(self) -> tuple[str, str]:
        inst = self.gain_instance
        return f"effect_{inst}:Out1", f"effect_{inst}:Out2"

    def _instrument_midi_port(self, item: Instrument) -> str:
        return item.midi_port

    def activate(self) -> None:
        self.client.activate()
        out_l, out_r = self._gain_ports()
        self.client.connect(out_l, self.audio_l)
        self.client.connect(out_r, self.audio_r)

    def deactivate(self) -> None:
        try:
            self.client.deactivate()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass

    def _select_instrument(self, active_id: int) -> None:
        for item in self.board.instruments:
            mod_bypass(item.instance_id, item.instance_id != active_id)
        mod_param_set(self.gain_instance, OUTPUT_GAIN_PARAM, 0.0)

    def _play_test_chord(self) -> None:
        notes = self.args.notes
        channel = self.args.channel
        velocity = self.args.velocity

        for note in notes:
            self.send_q.put(midi_note_on(note, velocity, channel))
        time.sleep(self.args.hold)
        for note in notes:
            self.send_q.put(midi_note_off(note, channel))
        time.sleep(0.1)

    def measure_instrument(self, item: Instrument) -> float:
        midi_port = self._instrument_midi_port(item)
        try:
            self.client.connect(self.midi_out, midi_port)
        except jack.JackError as exc:
            raise RuntimeError(f"cannot connect MIDI to {midi_port}: {exc}") from exc

        try:
            self._select_instrument(item.instance_id)
            time.sleep(self.args.settle)
            self.meter.reset()
            self._play_test_chord()
            time.sleep(self.args.tail)
            return self.meter.rms()
        finally:
            try:
                self.client.disconnect(self.midi_out, midi_port)
            except jack.JackError:
                pass

    def recommend_gain(self, measured_rms: float) -> float:
        measured_db = amp_to_db(measured_rms, self.args.floor_db)
        return self.args.target - measured_db

    def run(self, instruments: list[Instrument]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        self.activate()
        try:
            for item in instruments:
                print(f"== Measuring {item.instance_id} {item.symbol} ==")
                try:
                    measured_rms = self.measure_instrument(item)
                    measured_db = amp_to_db(measured_rms, self.args.floor_db)
                    if measured_rms <= 0.0 or measured_db <= self.args.floor_db + 0.5:
                        print("   FAILED: no measurable audio (check MIDI routing and bypass)")
                        continue
                    recommended = self.recommend_gain(measured_rms)
                    recommended = round(recommended / self.args.round_db) * self.args.round_db
                    delta = recommended - item.current_gain
                    row = {
                        "id": item.instance_id,
                        "symbol": item.symbol,
                        "measured_db": measured_db,
                        "current_gain": item.current_gain,
                        "recommended_gain": recommended,
                        "delta": delta,
                    }
                    results.append(row)
                    print(
                        f"   measured {measured_db:5.1f} dBFS RMS  "
                        f"current {item.current_gain:+5.1f} dB  "
                        f"recommend {recommended:+5.1f} dB  "
                        f"(delta {delta:+5.1f} dB)"
                    )
                except Exception as exc:
                    print(f"   FAILED: {exc}", file=sys.stderr)
        finally:
            self.deactivate()
        return results


def print_summary(results: list[dict[str, Any]], target: float) -> None:
    if not results:
        print("\nNo measurements collected.")
        return

    print("\n== Gain recommendations ==")
    print(f"Target post-gain RMS: {target:.1f} dBFS (Gain2x2 at 0 dB during test)\n")
    print(f"{'ID':>4}  {'Symbol':<22} {'Meas':>8} {'Current':>8} {'Rec':>8} {'Delta':>8}")
    print("-" * 66)
    for row in results:
        print(
            f"{row['id']:4d}  "
            f"{row['symbol']:<22} "
            f"{row['measured_db']:7.1f}  "
            f"{row['current_gain']:+7.1f}  "
            f"{row['recommended_gain']:+7.1f}  "
            f"{row['delta']:+7.1f}"
        )

    print("\nCopy into your pedalboard JSON as top-level \"gain\" per instrument.")
    print('Example:  "16": { "symbol": "sfizz-rhodes", "gain": 0.0, ... }')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recommend per-instrument JSON gain values.")
    parser.add_argument("pedalboard", type=Path, help="path to pedalboard JSON")
    parser.add_argument("--target", type=float, default=-18.0, help="target post-gain RMS in dBFS")
    parser.add_argument("--velocity", type=int, default=DEFAULT_VELOCITY, help="test note velocity")
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL, help="MIDI channel 0-15")
    parser.add_argument(
        "--notes",
        type=int,
        nargs="+",
        default=list(DEFAULT_NOTES),
        help="MIDI note numbers for test chord",
    )
    parser.add_argument("--settle", type=float, default=1.0, help="seconds after bypass before test")
    parser.add_argument("--hold", type=float, default=2.0, help="seconds to hold test chord")
    parser.add_argument("--tail", type=float, default=0.25, help="seconds after note-off to keep measuring")
    parser.add_argument("--floor-db", type=float, default=-60.0, help="dB floor for silence")
    parser.add_argument("--round-db", type=float, default=0.5, help="round recommendations to this step")
    parser.add_argument(
        "--instrument",
        type=int,
        action="append",
        dest="instruments",
        help="only calibrate this instance id (repeatable)",
    )
    parser.add_argument("--no-load", action="store_true", help="assume pedalboard is already loaded")
    parser.add_argument("--no-cleanup", action="store_true", help="do not remove plugins when finished")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        pb = json.loads(args.pedalboard.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"pedalboard not found: {args.pedalboard}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1

    board = parse_pedalboard(pb)
    if not board.instruments:
        print("no sampler instruments found in pedalboard", file=sys.stderr)
        return 1

    instruments = board.instruments
    if args.instruments:
        wanted = set(args.instruments)
        instruments = [item for item in instruments if item.instance_id in wanted]
        if not instruments:
            print(f"none of {sorted(wanted)} are sampler instruments", file=sys.stderr)
            return 1

    loaded_ids: list[int] = []
    active_connections: list[tuple[str, str]] = []
    we_loaded = False

    if not args.no_load:
        try:
            loaded_ids, active_connections = load_pedalboard(board)
            we_loaded = True
        except Exception as exc:
            print(f"failed to load pedalboard: {exc}", file=sys.stderr)
            return 1

    try:
        calibrator = Calibrator(board, args)
        results = calibrator.run(instruments)
        print_summary(results, args.target)
    except Exception as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if we_loaded and not args.no_cleanup:
            cleanup_session(loaded_ids, active_connections)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
