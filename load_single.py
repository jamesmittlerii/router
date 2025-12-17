#!/usr/bin/env python3
"""
Headless MOD-style pedalboard.json loader for mod-host.
(Single Client Version)

- Reads a pedalboard.json (MOD v2-ish) containing:
  - plugins: { "<id>": { "uri": "...", "controls": {...}, "state": {...} } }
  - connections: [ { "from": "...", "to": "..." }, ... ]

- Talks to mod-host over TCP (default 127.0.0.1:5555) using its text protocol.
- STARTS JACK CLIENT AFTER LOADING to listen for MIDI Program Changes.
- Switches plugins 10-16 based on Program Change 0-6.
- Syncs SL88 keyboard at startup using the SAME client.

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

# ---- JACK MIDI Handling ----

event_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)  # Incoming
send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=128)   # Outgoing

client = jack.Client("Router_Loader")
in_port = client.midi_inports.register("input")
out_port = client.midi_outports.register("output")

@client.set_process_callback
def process(frames):
    # 1) Incoming MIDI
    for offset, data in in_port.incoming_midi_events():
        try:
            event_q.put_nowait(bytes(data))
        except queue.Full:
            pass
            
    # 2) Outgoing MIDI
    # Process all queued outgoing messages
    while True:
        try:
            # We write at offset 0 to send immediately in this cycle
            msg = send_q.get_nowait()
            out_port.write_midi_event(0, msg)
        except queue.Empty:
            break

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

    # Track resources for cleanup
    loaded_ids: list[int] = []
    active_connections: list[tuple[str, str]] = []

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
            loaded_ids.append(inst)
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
            active_connections.append((src, dst))
        except Exception as e:
             print(f"Failed connect {src}->{dst}: {e}")

    print("== done loading ==")
    print("---------------------------------------------------")
    print(f"Started JACK client: {client.name}")
    
    # Activate JACK Client
    try:
        client.activate()
    except Exception as e:
        print(f"Failed to activate JACK client: {e}")
        return

    # 4) Sync SL88 (Using Main Client)
    if active_piano is not None:
        print(f"[SL88 Sync] Attempting to sync SL88 to active piano {active_piano}...")
        
        target_port_name = "system:midi_playback_1"
        try:
            sl_dest = client.get_port_by_name(target_port_name)
            
            if sl_dest:
                print(f"[SL88 Sync] Found destination: {sl_dest.name}")
                client.connect(out_port, sl_dest)
                print(f"[SL88 Sync] Connected {out_port.name} -> {sl_dest.name}")
                
                # Send Program Change
                # Channel 2 (0-indexed 1) -> 0xC1
                status = 0xC0 | (COMMON_CHANNEL - 1)
                msg_bytes = bytes([status, active_piano])
                
                # Use the Queue to send exactly once
                send_q.put(msg_bytes)
                print(f"[SL88 Sync] Queued ONE-SHOT Program Change: {active_piano} on Ch{COMMON_CHANNEL} (Hex: {msg_bytes.hex()})")
                
                # Wait briefly to ensure the processing thread picks it up
                time.sleep(1.0)
                
                # DISCONNECT to prevent feedback loops/flickering
                print(f"[SL88 Sync] Disconnecting {out_port.name} -> {sl_dest.name} to avoid loops.")
                try:
                    client.disconnect(out_port, sl_dest)
                except Exception as e:
                    print(f"[SL88 Sync] Warning during disconnect: {e}")
            else:
                print(f"[SL88 Sync] Warning: Could not find JACK port '{target_port_name}'")
                
        except Exception as e:
             print(f"[SL88 Sync] Failed: {e}")

    print("Starting JACK MIDI listener for Program Changes...")
    print(f"Listening on: {client.name}:input")

    # Try to auto-connect for Input
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
            print(f"Received: {msg!r}")


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
            if prog in piano_ids:
                print(f"   Selecting Piano {prog}...")
                
                for inst in piano_ids:
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
        print("\n== Cleaning Up Session ==")
        
        # 1. Disconnect ports
        for src, dst in reversed(active_connections):
            try:
                # We use send_cmd directly to avoid our helper strict checks if desired,
                # but standard cleanup is fine.
                print(f"Disconnecting {src} -> {dst}")
                send_cmd(f'disconnect "{src}" "{dst}"')
            except Exception as e:
                print(f"Failed to disconnect {src}->{dst}: {e}")
                
        # 2. Remove plugins
        for inst in reversed(loaded_ids):
            try:
                print(f"Removing plugin {inst}")
                send_cmd(f"remove {inst}")
            except Exception as e:
                print(f"Failed to remove plugin {inst}: {e}")

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
