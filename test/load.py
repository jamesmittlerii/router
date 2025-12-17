#!/usr/bin/env python3
"""
Headless MOD-style pedalboard.json loader for mod-host.

- Reads a pedalboard.json (MOD v2-ish) containing:
  - plugins: { "<id>": { "uri": "...", "controls": {...}, "state": {...} } }
  - connections: [ { "from": "...", "to": "..." }, ... ]

- Talks to mod-host over TCP (default 127.0.0.1:5555) using its text protocol.
- STARTS JACK CLIENT AFTER LOADING to listen for MIDI Program Changes.
- Switches plugins 10-16 based on Program Change 0-6.

Compatibility notes:
- In some mod-host builds, `add "<uri>" <id>` returns `resp <id>` (NOT resp 0).
- Most setters/connect return `resp 0` on success.
- Errors are typically negative (e.g. resp -101).
"""

import json
import os
import socket
import sys
import time
import queue
import threading
from pathlib import Path
from typing import Any, Optional

import jack
import mido

# ---- Configuration ----

MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))
COMMON_CHANNEL = 2  # User confirmed Channel 2

# Which JACK MIDI source to tap for Program Changes
TARGET_PORT = "system:midi_capture_1"
FILTER_CHANNEL = None  # Set to 0-15 to filter by channel, or None for all

# ---- Helper Functions ----

def send_cmd(line: str) -> str:
    """
    Send one mod-host command, return response text (NUL bytes removed).
    """
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

    # mod-host may include NUL bytes (you saw this in bash as "ignored null byte")
    resp = resp.replace(b"\x00", b"")
    return resp.decode("utf-8", errors="replace").strip()


def parse_resp(resp: str) -> Optional[int]:
    """
    Parse 'resp <int>' and return the int, else None.
    """
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
    """
    Accept any non-negative resp code as success; return code.
    """
    code = parse_resp(resp)
    if code is None:
        raise RuntimeError(f"{what} failed (unparseable): {resp}")
    if code < 0:
        raise RuntimeError(f"{what} failed: {resp}")
    return code


def expect_zero(resp: str, what: str) -> None:
    """
    Success iff resp == 0.
    """
    code = parse_resp(resp)
    if code != 0:
        raise RuntimeError(f"{what} failed: {resp}")


def mod_preload(uri: str, instance_id: int) -> None:
    resp = send_cmd(f'preload "{uri}" {instance_id}')
    code = expect_nonnegative(resp, f'add {instance_id} {uri}')
    # Many builds return the created instance id.
    if code != instance_id:
        print(f"WARNING: add requested id={instance_id} but host returned resp {code}")


def mod_bypass(inst: int, bypass_on: bool) -> None:
    # bypass_on=True  -> "bypass <inst> 1"
    # bypass_on=False -> "bypass <inst> 0"
    resp = send_cmd(f"bypass {inst} {1 if bypass_on else 0}")
    expect_zero(resp, f"bypass {inst}")

def mod_add(uri: str, instance_id: int) -> None:
    resp = send_cmd(f'add "{uri}" {instance_id}')
    code = expect_nonnegative(resp, f'add {instance_id} {uri}')
    # Many builds return the created instance id.
    if code != instance_id:
        print(f"WARNING: add requested id={instance_id} but host returned resp {code}")


def mod_param_set(instance_id: int, symbol: str, value: Any) -> None:
    # param_set expects scalar values; keep as-is (numbers ok)
    resp = send_cmd(f"param_set {instance_id} {symbol} {value}")
    expect_zero(resp, f"param_set {instance_id} {symbol}")


def mod_patch_set(instance_id: int, key: str, value: str) -> None:
    # patch_set expects quoted key and quoted value
    resp = send_cmd(f'patch_set {instance_id} "{key}" "{value}"')
    expect_zero(resp, f"patch_set {instance_id} {key}")


def mod_connect(src: str, dst: str) -> None:
    resp = send_cmd(f'connect "{src}" "{dst}"')
    expect_zero(resp, f"connect {src} -> {dst}")


def expand_port(port: str) -> str:
    """
    Convert pedalboard shorthand "40:out_left" to mod-host "effect_40:out_left".
    Leave system:*, mod-host:* etc untouched.
    """
    if ":" in port:
        left, right = port.split(":", 1)
        if left.isdigit():
            return f"effect_{left}:{right}"
    return port

# ---- Helper Functions ----

def sync_sl88(program: int):
    """
    Spins up a temporary JACK client to send a single Program Change message
    to the SL88 keyboard (via system:midi_playback_1), then exits.
    """
    print(f"[SL88 Sync] Starting temporary client to set program={program}...")
    
    try:
        tmp_client = jack.Client("Router_Sync")
        out = tmp_client.midi_outports.register("out")
        done = threading.Event()
        sent = False
        
        # We need to capture 'program' in the closure
        # 0xC0 | (2-1) = 0xC1 for Channel 2
        status = 0xC0 | (COMMON_CHANNEL - 1)
        # Using a list for the bytes to ensure it's mutable if needed, passed to bytes()
        msg_bytes = bytes([status, program])

        @tmp_client.set_process_callback
        def process(frames):
            nonlocal sent
            if sent:
                return
            # Write immediately at offset 0
            out.write_midi_event(0, msg_bytes)
            sent = True
            done.set()
        
        print("[SL88 Sync] Activating client...")
        tmp_client.activate()
        
        target_port_name = "system:midi_playback_1"
        try:
            target = tmp_client.get_port_by_name(target_port_name)
            if target:
                print(f"[SL88 Sync] Connecting to {target_port_name}...")
                tmp_client.connect(out, target)
                
                print("[SL88 Sync] Waiting for process cycle to send...")
                # Wait up to 2 seconds for JACK to cycle
                if done.wait(2.0):
                    print(f"[SL88 Sync] SUCCESS: Sent Program Change {program} on Ch{COMMON_CHANNEL} (Hex: {msg_bytes.hex()})")
                    # Give JACK audio thread a moment to flush the buffer before we close
                    time.sleep(0.25)
                else:
                    print("[SL88 Sync] TIMEOUT: Did not send message within 2.0s")
            else:
                print(f"[SL88 Sync] ERROR: Could not find JACK port '{target_port_name}'")
        except jack.JackError as e:
            print(f"[SL88 Sync] JackError during connection: {e}")
            
    except Exception as e:
        print(f"[SL88 Sync] Exception: {e}")
    finally:
        # Clean up
        try:
            if 'tmp_client' in locals() and tmp_client:
                tmp_client.deactivate()
                tmp_client.close()
                print("[SL88 Sync] Client closed.")
        except Exception as e:
            pass

# ---- JACK MIDI Handling ----

event_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)

# (No more global one-shot state needed here)

client = jack.Client("Router_Loader")
in_port = client.midi_inports.register("input")
# (No output port needed on the main listener)

@client.set_process_callback
def process(frames):
    # 1) Incoming MIDI
    for offset, data in in_port.incoming_midi_events():
        try:
            event_q.put_nowait(bytes(data))
        except queue.Full:
            pass
            
    # No outgoing MIDI logic here anymore

def decode_mido(event_bytes: bytes):
    """Decode raw MIDI bytes into a mido Message if possible."""
    try:
        return mido.Message.from_bytes(event_bytes)
    except ValueError:
        return None

# ---- Main ----

def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/pedalboard.json", file=sys.stderr)
        sys.exit(2)

    pb_path = Path(sys.argv[1])
    try:
        pb = json.loads(pb_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Error: File not found: {pb_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {pb_path}: {e}")
        sys.exit(1)

    plugins: dict[str, Any] = pb.get("plugins", {})
    connections: list[dict[str, str]] = pb.get("connections", [])

    print("== Loading Plugins == ")
    piano_ids = []
    active_piano = None

    # 1) Add plugins (sorted by numeric id for deterministic behavior)
    for sid in sorted(plugins.keys(), key=lambda x: int(x)):
        p = plugins[sid]
        uri = p["uri"]
        inst = int(sid)
        if uri == "http://sfztools.github.io/sfizz":
            piano_ids.append(inst)

        print(f'== add {inst} {uri}')
        try:
            mod_add(uri, inst)
        except Exception as e:
            print(f"Failed to add plugin {inst}: {e}")

    # 2) Apply state (patch_set) and controls (param_set)
    print("== Applying State & Controls ==")
    for sid in sorted(plugins.keys(), key=lambda x: int(x)):
        p = plugins[sid]
        inst = int(sid)

        state = p.get("state", {}) or {}
        for key, val in state.items():
            print(f"== patch_set {inst} {key} = {val}")
            try:
                mod_patch_set(inst, key, str(val))
            except Exception as e:
                print(f"Failed patch_set {inst} {key}: {e}")

        controls = p.get("controls", {}) or {}
        for symbol, val in controls.items():
            print(f"== param_set {inst} {symbol} {val}")
            try:
                mod_param_set(inst, symbol, val)
            except Exception as e:
                print(f"Failed param_set {inst} {symbol}: {e}")

        # 3) Optional bypass flag (boolean)
        if "bypass" in p:
            bypass_on = bool(p["bypass"])
            print(f"== bypass {inst} {1 if bypass_on else 0}")
            try:
                mod_bypass(inst, bypass_on)
                if not bypass_on and inst in piano_ids:
                    active_piano = inst
            except Exception as e:
                 print(f"Failed bypass {inst}: {e}")

    # Small delay helps samplers settle before wiring audio
    time.sleep(0.2)

    # 3) Connect ports
    print("== Connecting Ports ==")
    for c in connections:
        src = expand_port(c["from"])
        dst = expand_port(c["to"])
        print(f"== connect {src} -> {dst}")
        try:
            mod_connect(src, dst)
        except Exception as e:
             print(f"Failed connect {src}->{dst}: {e}")

    print("== done loading ==")
    print("---------------------------------------------------")
    
    # 4) Sync SL88 (Temporary dedicated client)
    if active_piano is not None:
        sync_sl88(active_piano)

    print("Starting JACK MIDI listener for Program Changes...")
    print(f"Started JACK client: {client.name}")
    # (Client already activated above)
    # Activate JACK Client
    try:
        client.activate()
    except Exception as e:
        print(f"Failed to activate JACK client: {e}")
        return

    print(f"Listening on: {client.name}:input")

    # Try to auto-connect
    try:
        src_port = client.get_port_by_name(TARGET_PORT)
        if src_port:
            client.connect(src_port, in_port)
            print(f"Connected {TARGET_PORT} -> {client.name}:input")
        else:
            print(f"Warning: Could not find '{TARGET_PORT}'. Connect manually, e.g.:")
            print(f"  jack_connect {TARGET_PORT} {client.name}:input")
    except jack.JackError as e:
        print(f"Connection error: {e}")

    print("Listening for MIDI events... (Ctrl+C to stop)")
    print(f"Mapping: Program Change X -> Piano Instance X. Detected Pianos: {sorted(piano_ids)}")

    last_prog = None

    try:
        while True:
            try:
                data = event_q.get(timeout=1.0)
            except queue.Empty:
                continue

            msg = decode_mido(data)
            if msg is None:
                continue

            # Debug print
            # print(f"Received: {msg!r}")

            if msg.type != "program_change":
                continue

            if FILTER_CHANNEL is not None and msg.channel != FILTER_CHANNEL:
                continue

            prog = msg.program

            # Optional debounce
            if prog == last_prog:
                continue
            last_prog = prog

            print(f"🎹 PROGRAM CHANGE -> program={prog}, channel={msg.channel}")

            # Mapping Logic
            # If the program number matches one of our known piano instances,
            # enable that one and bypass the others.
            
            if prog in piano_ids:
                print(f"   Selecting Piano {prog}...")
                
                for inst in piano_ids:
                    # If this is the one we want, bypass=False (active)
                    # If this is NOT the one, bypass=True (bypassed)
                    should_be_active = (inst == prog)
                    bypass_val = False if should_be_active else True
                    
                    try:
                         mod_bypass(inst, bypass_val)
                    except Exception as e:
                        print(f"   Failed to set bypass for {inst}: {e}")
            else:
                print(f"   (Program {prog} is not a known piano instance, ignoring switch)")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            client.deactivate()
        except:
            pass
        try:
            client.close()
        except:
             pass


if __name__ == "__main__":
    main()
