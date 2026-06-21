#!/usr/bin/env python3
"""
Upload an LCXL3 custom-mode layout from a pedalboard.json plugin entry.

Reads the plugin's ``lcxl`` section, builds the
same SysEx payload as load_single.py, and sends it to the LCXL3 DAW-In port.

Works with the LCXL3 connected directly to this PC (rtmidi) or via JACK.

Examples:
  python test/lcxl3_setup_plugin.py --list-ports
  python test/lcxl3_setup_plugin.py 26
  python test/lcxl3_setup_plugin.py 26 --pedalboard jsons/plus.json
  python test/lcxl3_setup_plugin.py 26 --dry-run
  python test/lcxl3_setup_plugin.py 26 --listen 15
  python test/lcxl3_setup_plugin.py 28 --backend jack
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lcxl3 import (  # noqa: E402
    LIVE_CUSTOM_MODE_SLOT,
    build_custom_mode_messages,
    build_global_channel_midi_messages,
    build_slot_select_sysex,
    parse_lcxl_layout,
)

DEFAULT_PEDALBOARD = ROOT / "jsons" / "plus.json"
COMMON_CHANNEL = 2


def load_plugin(pedalboard_path: Path, instance: int) -> dict[str, Any]:
    data = json.loads(pedalboard_path.read_text(encoding="utf-8"))
    plugins = data.get("plugins", {})
    key = str(instance)
    if key not in plugins:
        known = ", ".join(sorted(plugins.keys(), key=int))
        raise SystemExit(
            f"Plugin instance {instance} not in {pedalboard_path.name}."
            f" Known instances: {known}"
        )
    plugin = plugins[key]
    if not isinstance(plugin, dict):
        raise SystemExit(f"Plugin {instance} is not an object")
    return plugin


def resolve_channel(_plugin: dict[str, Any], override: Optional[int]) -> int:
    if override is not None:
        return override
    return COMMON_CHANNEL


def cc_labels(plugin: dict[str, Any]) -> dict[int, str]:
    labels: dict[int, str] = {}
    midi_cc = plugin.get("midi_cc")
    if not isinstance(midi_cc, dict):
        return labels
    for cc_key, entry in midi_cc.items():
        try:
            cc_num = int(cc_key)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("param") or entry.get("symbol")
            if label:
                labels[cc_num] = str(label)
        elif isinstance(entry, str):
            labels[cc_num] = entry
    return labels


def print_layout_summary(
    plugin: dict[str, Any],
    layout: dict[str, list],
    channel: int,
) -> None:
    symbol = plugin.get("symbol", "?")
    labels = cc_labels(plugin)

    def line(kind: str, index: int, cc: int) -> None:
        name = labels.get(cc, "")
        suffix = f" - {name}" if name else ""
        print(f"  {kind} {index + 1}: CC {cc}{suffix}")

    print(f"Instrument: {symbol} (instance {plugin.get('_instance', '?')})")
    print(f"LCXL3 MIDI channel: {channel}")
    print("Faders:")
    for idx, cc in enumerate(layout["faders"]):
        line("Fader", idx, cc)
    if layout["encoders"]:
        print("Encoders:")
        for row_idx, row in enumerate(layout["encoders"]):
            for col_idx, cc in enumerate(row):
                line(f"Enc row {row_idx + 1}", col_idx + row_idx * 8, cc)
    buttons = layout.get("buttons") or []
    if any(b is not None for b in buttons):
        print("Fader buttons:")
        for idx, cc in enumerate(buttons):
            if cc is None:
                continue
            line("Button", idx, cc)


# ---- rtmidi (direct USB on Windows / ALSA on Linux) ----


def list_rtmidi_ports() -> None:
    try:
        import rtmidi
    except ImportError:
        print("rtmidi not installed (pip install python-rtmidi)", file=sys.stderr)
        raise SystemExit(1)

    midi_out = rtmidi.MidiOut()
    midi_in = rtmidi.MidiIn()
    print("MIDI OUT ports (use one containing 'DAW' and 'In' for SysEx upload):")
    for idx, name in enumerate(midi_out.get_ports()):
        print(f"  [{idx}] {name}")
    print("\nMIDI IN ports (listen for CC verification):")
    for idx, name in enumerate(midi_in.get_ports()):
        print(f"  [{idx}] {name}")


def pick_rtmidi_port(
    ports: list[str],
    *,
    direction: str,
    explicit: Optional[str],
) -> int:
    if explicit is not None:
        for idx, name in enumerate(ports):
            if explicit.lower() in name.lower():
                return idx
        raise SystemExit(f"Port not found containing {explicit!r}")

    lowered = [p.lower() for p in ports]

    def is_lcxl(name: str) -> bool:
        return "lcxl" in name or "launch control xl" in name

    if direction == "out":
        # Prefer explicit DAW-In naming (Linux/JACK aliases).
        for idx, name in enumerate(lowered):
            if is_lcxl(name) and "daw" in name and "in" in name:
                return idx
        # Windows: second LCXL3 virtual cable is usually DAW In (MIDIOUT2).
        for idx, name in enumerate(lowered):
            if is_lcxl(name) and "midiout2" in name.replace(" ", ""):
                return idx
        lcxl_indices = [idx for idx, name in enumerate(lowered) if is_lcxl(name)]
        if len(lcxl_indices) >= 2:
            return lcxl_indices[1]
        if lcxl_indices:
            return lcxl_indices[0]
    else:
        for idx, name in enumerate(lowered):
            if is_lcxl(name) and "daw" in name and "out" in name:
                return idx
        # Custom-mode performance CCs use the main USB MIDI port (not DAW Out).
        for idx, name in enumerate(lowered):
            if is_lcxl(name) and "midiin2" not in name.replace(" ", ""):
                return idx
        for idx, name in enumerate(lowered):
            if is_lcxl(name) and "midiin2" in name.replace(" ", ""):
                return idx
        lcxl_indices = [idx for idx, name in enumerate(lowered) if is_lcxl(name)]
        if lcxl_indices:
            return lcxl_indices[0]

    raise SystemExit(
        f"No LCXL3 {direction} port found. Run with --list-ports and --out-port / --in-port."
    )


def send_rtmidi_sysex(
    payload: bytes,
    *,
    out_port_hint: Optional[str],
) -> str:
    import rtmidi

    midi_out = rtmidi.MidiOut()
    ports = midi_out.get_ports()
    if not ports:
        raise SystemExit("No MIDI OUT ports found.")
    idx = pick_rtmidi_port(ports, direction="out", explicit=out_port_hint)
    midi_out.open_port(idx)
    port_name = ports[idx]
    midi_out.send_message(list(payload))
    time.sleep(0.05)
    return port_name


def _unpack_rtmidi_message(raw) -> Optional[list[int]]:
    """Normalize get_message() / callback payloads across python-rtmidi versions."""
    if raw is None:
        return None
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    return [int(b) & 0xFF for b in raw]


def _format_midi_message(data: list[int]) -> str:
    status = data[0]
    rest = data[1:]
    msg_type = status & 0xF0
    ch = (status & 0x0F) + 1
    if msg_type == 0xB0 and len(rest) >= 2:
        return f"CC ch{ch} cc={rest[0]} val={rest[1]}"
    if msg_type == 0x90 and len(rest) >= 2:
        return f"NoteOn ch{ch} note={rest[0]} vel={rest[1]}"
    if msg_type == 0x80 and len(rest) >= 2:
        return f"NoteOff ch{ch} note={rest[0]} vel={rest[1]}"
    return f"raw status=0x{status:02x} data={' '.join(str(b) for b in rest)}"


def listen_rtmidi_all_ports(
    seconds: float,
    *,
    expect_cc: Optional[set[int]],
    verbose: bool = False,
) -> bool:
    """Poll every MIDI IN port for messages (reliable on Windows; avoids callback issues)."""
    import rtmidi

    probe = rtmidi.MidiIn()
    ports = probe.get_ports()
    del probe
    if not ports:
        print("No MIDI IN ports; skipping listen.", file=sys.stderr)
        return False

    lcxl_indices = [
        idx
        for idx, name in enumerate(ports)
        if "lcxl" in name.lower() or "launch control xl" in name.lower()
    ]
    if not lcxl_indices:
        lcxl_indices = list(range(len(ports)))

    print(f"\nListening on {len(lcxl_indices)} port(s) for {seconds:.0f}s (polling)...")
    for idx in lcxl_indices:
        print(f"  [{idx}] {ports[idx]}")
    print("Move a fader or knob now.\n")

    seen: dict[tuple[int, str], int] = {}  # (port_idx, summary) -> val
    opened: list[tuple[int, str, rtmidi.MidiIn]] = []

    for idx in lcxl_indices:
        port_in = rtmidi.MidiIn()
        port_in.open_port(idx)
        # Positional args for older python-rtmidi (sysex, timing, active_sensing).
        port_in.ignore_types(False, False, True)
        opened.append((idx, ports[idx], port_in))

    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            for port_idx, port_name, port_in in opened:
                while True:
                    data = _unpack_rtmidi_message(port_in.get_message())
                    if data is None:
                        break
                    if data[0] == 0xF0:
                        if verbose:
                            print(f"  [{port_idx}] SysEx ({len(data)} bytes)")
                        continue
                    formatted = _format_midi_message(data)
                    if not verbose and formatted.startswith("raw "):
                        continue
                    key = (port_idx, formatted.rsplit(" val=", 1)[0])
                    if " val=" in formatted:
                        seen[key] = int(formatted.rsplit(" val=", 1)[1])
                    marker = ""
                    if expect_cc and formatted.startswith("CC "):
                        try:
                            cc_num = int(formatted.split(" cc=")[1].split()[0])
                            if cc_num in expect_cc:
                                marker = "  <-- expected"
                        except (IndexError, ValueError):
                            pass
                    print(f"  [{port_idx}] {port_name}: {formatted}{marker}")
            time.sleep(0.002)
    finally:
        for _idx, _name, port_in in opened:
            port_in.close_port()

    cc_seen = set()
    for (_port_idx, summary), _val in seen.items():
        if summary.startswith("CC "):
            try:
                cc_seen.add(int(summary.split(" cc=")[1].split()[0]))
            except (IndexError, ValueError):
                pass

    if expect_cc:
        missing = expect_cc - cc_seen
        if missing:
            print(f"\nDid not see CC(s): {sorted(missing)}")
            if cc_seen:
                print(f"Received CC number(s): {sorted(cc_seen)}")
            _print_no_usb_midi_help()
            return False
        print(f"\nSaw expected CC(s): {sorted(expect_cc & cc_seen)}")
        return True
    if seen:
        print(f"\nReceived {len(seen)} distinct MIDI message(s).")
        return True
    _print_no_usb_midi_help()
    return False


def _print_no_usb_midi_help() -> None:
    print("\nNo MIDI received on the PC.")
    print("The LCXL3 screen can still update (local display) without sending USB MIDI.")
    print("On the hardware: Shift + Mode (Edit menu):")
    print("  - Output Port: enable USB Main (or Main USB MIDI)")
    print("  - Confirm you are in the Custom Mode that was programmed")
    print("Then re-run:  python test/lcxl3_setup_plugin.py 26 --probe-only 15")


def listen_rtmidi_cc(
    seconds: float,
    *,
    in_port_hint: Optional[str],
    expect_cc: Optional[set[int]],
) -> bool:
    """Listen on one port (legacy). Prefer listen_rtmidi_all_ports."""
    if in_port_hint is None:
        return listen_rtmidi_all_ports(seconds, expect_cc=expect_cc, verbose=False)
    import rtmidi

    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    if not ports:
        print("No MIDI IN ports; skipping listen.", file=sys.stderr)
        return False

    idx = pick_rtmidi_port(ports, direction="in", explicit=in_port_hint)
    port_name = ports[idx]
    print(f"\nListening on [{idx}] {port_name} for {seconds:.0f}s — move a fader/knob...")
    seen: dict[tuple[int, int], int] = {}

    def callback(message, _data) -> None:
        data = _unpack_rtmidi_message(message)
        if data is None or (data[0] & 0xF0) != 0xB0 or len(data) < 3:
            return
        status, cc, val = data[0], data[1], data[2]
        ch = (status & 0x0F) + 1
        seen[(ch, cc)] = val
        marker = ""
        if expect_cc and cc in expect_cc:
            marker = "  <-- expected"
        print(f"  CC ch{ch} cc={cc} val={val}{marker}")

    midi_in.open_port(idx)
    midi_in.set_callback(callback)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        midi_in.cancel_callback()
        midi_in.close_port()

    if expect_cc:
        matched = {cc for (_ch, cc) in seen if cc in expect_cc}
        missing = expect_cc - matched
        if missing:
            print(f"\nDid not see CC(s): {sorted(missing)}")
            if seen:
                print("Received other CC(s):")
                for (ch, cc), val in sorted(seen.items()):
                    print(f"  ch{ch} cc={cc} val={val}")
            return False
        print(f"\nSaw expected CC(s): {sorted(matched)}")
        return True
    if seen:
        print(f"\nReceived {len(seen)} distinct CC message(s).")
        return True
    print("\nNo CC messages received.")
    return False


# ---- JACK ----


def send_jack_sysex(payload: bytes, *, daw_in: Optional[str]) -> str:
    import jack

    client = jack.Client("LCXL3_SetupTest", no_start_server=True)
    midi_out = client.midi_outports.register("out")
    client.activate()

    dest = client.get_port_by_name(daw_in) if daw_in else None
    if dest is None:
        for entry in client.get_ports():
            port = client.get_port_by_name(entry) if isinstance(entry, str) else entry
            if port is None:
                continue
            for alias in port.aliases:
                if "daw-in" in alias.lower():
                    dest = port
                    break
            if dest is not None:
                break
    if dest is None:
        client.close()
        raise SystemExit(
            "Could not find LCXL3 DAW-In JACK port. Use --daw-in or --backend rtmidi."
        )

    client.connect(midi_out, dest)
    midi_out.clear_buffer()
    midi_out.write_midi_event(0, payload)
    time.sleep(0.1)
    dest_name = dest.name
    client.deactivate()
    client.close()
    return dest_name


def build_messages(
    plugin: dict[str, Any],
    layout: dict[str, list],
    *,
    channel: int,
    slot: int,
    select_slot: Optional[int],
) -> list[tuple[str, bytes]]:
    symbol = str(plugin.get("symbol") or "router")
    labels = cc_labels(plugin)
    flat_encoders = [cc for row in layout["encoders"] for cc in row]
    messages: list[tuple[str, bytes]] = []
    if select_slot is not None:
        messages.append(
            (f"select custom mode slot {select_slot}", build_slot_select_sysex(select_slot))
        )
    for page_idx, payload in enumerate(
        build_custom_mode_messages(
            symbol[:14],
            faders=layout["faders"],
            encoders=flat_encoders,
            buttons=layout.get("buttons") or [],
            channel=channel,
            slot=slot,
            labels=labels,
        )
    ):
        messages.append((f"upload page {page_idx} '{symbol}'", payload))
    return messages


def send_messages(
    messages: list[tuple[str, bytes]],
    send_fn: Callable[[bytes], str],
) -> None:
    for label, payload in messages:
        dest = send_fn(payload)
        print(f"Sent {label} ({len(payload)} bytes) -> {dest}")
        print(f"  hex: {payload[:24].hex(' ')}{'...' if len(payload) > 24 else ''}")
        time.sleep(0.08)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload LCXL3 custom mode from a pedalboard.json plugin lcxl section",
    )
    parser.add_argument(
        "instance",
        nargs="?",
        type=int,
        help="Plugin instance id (e.g. 26 for Caveman Cosmonaut)",
    )
    parser.add_argument(
        "--pedalboard",
        type=Path,
        default=DEFAULT_PEDALBOARD,
        help=f"Pedalboard JSON (default: {DEFAULT_PEDALBOARD})",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List rtmidi ports and exit",
    )
    parser.add_argument(
        "--backend",
        choices=("rtmidi", "jack", "auto"),
        default="auto",
        help="MIDI backend (default: auto = rtmidi if available else jack)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        help=f"LCXL3 output MIDI channel (default: {COMMON_CHANNEL})",
    )
    parser.add_argument(
        "--slot",
        type=int,
        default=LIVE_CUSTOM_MODE_SLOT,
        help=(
            f"Custom-mode write target in SysEx (default: {LIVE_CUSTOM_MODE_SLOT} = live/active mode; "
            "0-14 = stored custom mode slot)"
        ),
    )
    parser.add_argument(
        "--select-slot",
        type=int,
        help="Select custom mode before upload (0 = Custom Mode 1). Omit unless writing a stored slot.",
    )
    parser.add_argument(
        "--out-port",
        help="rtmidi OUT port substring (default: auto-detect LCXL3 DAW In)",
    )
    parser.add_argument(
        "--in-port",
        help="rtmidi IN port substring for --listen (default: auto-detect)",
    )
    parser.add_argument(
        "--daw-in",
        help="JACK port name for LCXL3 DAW-In (backend=jack)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print layout and SysEx size only; do not send",
    )
    parser.add_argument(
        "--listen",
        type=float,
        metavar="SECONDS",
        help="After upload, listen for CC on LCXL3 output (rtmidi only)",
    )
    parser.add_argument(
        "--probe-only",
        type=float,
        metavar="SECONDS",
        help="Skip upload; only listen for CC on all LCXL3 input ports",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print all incoming MIDI message types while listening",
    )
    parser.add_argument(
        "--expect-cc",
        type=int,
        nargs="*",
        help="Only pass listen/probe if these CC numbers are seen (default: any CC)",
    )
    parser.add_argument(
        "--save-syx",
        type=Path,
        help="Write generated .syx file (for Novation Components upload)",
    )
    args = parser.parse_args()

    if args.list_ports:
        list_rtmidi_ports()
        raise SystemExit(0)

    if args.instance is None:
        parser.error("instance is required (e.g. 26) unless --list-ports is used")

    pedalboard = args.pedalboard
    if not pedalboard.is_file():
        raise SystemExit(f"Pedalboard not found: {pedalboard}")

    plugin = load_plugin(pedalboard, args.instance)
    plugin["_instance"] = args.instance
    layout = parse_lcxl_layout(plugin.get("lcxl"))
    if layout is None:
        raise SystemExit(
            f"Plugin {args.instance} ({plugin.get('symbol', '?')}) has no usable 'lcxl' section"
        )

    channel = resolve_channel(plugin, args.channel)
    print_layout_summary(plugin, layout, channel)
    print()

    if args.probe_only:
        expect = set(args.expect_cc) if args.expect_cc else None
        ok = listen_rtmidi_all_ports(
            args.probe_only,
            expect_cc=expect,
            verbose=args.verbose,
        )
        raise SystemExit(0 if ok else 1)

    messages = build_messages(
        plugin,
        layout,
        channel=channel,
        slot=args.slot,
        select_slot=args.select_slot,
    )
    for label, payload in messages:
        print(f"Prepared {label}: {len(payload)} bytes")

    if args.dry_run:
        for label, payload in messages:
            print(f"  {label}: {payload[:16].hex(' ')}...")
        raise SystemExit(0)

    if args.save_syx:
        blob = b"".join(payload for _label, payload in messages)
        args.save_syx.write_bytes(blob)
        print(f"Wrote {len(blob)} bytes to {args.save_syx}")

    backend = args.backend
    if backend == "auto":
        try:
            import rtmidi  # noqa: F401

            backend = "rtmidi"
        except ImportError:
            backend = "jack"

    if backend == "rtmidi":
        def send_fn(payload: bytes) -> str:
            return send_rtmidi_sysex(payload, out_port_hint=args.out_port)

        send_messages(messages, send_fn)
        for idx, payload in enumerate(build_global_channel_midi_messages(channel)):
            dest = send_fn(payload)
            print(
                f"Sent global channel ch{channel} setup {idx + 1} "
                f"({payload.hex(' ')}) -> {dest}"
            )
        ok = True
        if args.listen:
            expect = set(args.expect_cc) if args.expect_cc else None
            ok = listen_rtmidi_all_ports(
                args.listen,
                expect_cc=expect,
                verbose=args.verbose,
            )
    else:
        def send_fn(payload: bytes) -> str:
            return send_jack_sysex(payload, daw_in=args.daw_in)

        send_messages(messages, send_fn)
        ok = True
        if args.listen:
            print("Note: --listen requires --backend rtmidi", file=sys.stderr)

    print("\nDone. On the LCXL3:")
    print("  1. Be in the Custom Mode that received the upload (name should match).")
    print("  2. Shift+Mode (Edit) > Output Port: turn ON USB Main.")
    print(f"  3. Global Channel should be ch{channel}.")
    print(f"  4. Move a fader and confirm CC on ch{channel} (router listens on the same channel).")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
