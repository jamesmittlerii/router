#!/usr/bin/env python3
import jack
import mido
import time
import socket
import queue
import threading

MOD_HOST = ("127.0.0.1", 5555)

# Map MIDI program number -> list of mod-host commands
PROGRAM_MAP = {
    0: ["bypass 0 0", "bypass 1 1", "bypass 2 1"],
    1: ["bypass 0 1", "bypass 1 0", "bypass 2 1"],
    2: ["bypass 0 1", "bypass 1 1", "bypass 2 0"],
}

# Which JACK MIDI source to tap
TARGET_PORT = "system:midi_capture_1"

# If you want to only react to a specific MIDI channel, set to 0..15; else None
FILTER_CHANNEL = None  # e.g. 1 to match your earlier "c1 .." observations

# ---- Mod-host control ----

_mod_lock = threading.Lock()

def send_modhost(cmd: str) -> None:
    """Send a single command line to mod-host."""
    # Keep this OUT of the JACK callback
    with _mod_lock:
        with socket.create_connection(MOD_HOST, timeout=0.5) as s:
            s.sendall((cmd + "\n").encode("utf-8"))

# ---- JACK MIDI capture ----

event_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)

client = jack.Client("Midi_Sniffer")
in_port = client.midi_inports.register("input")

@client.set_process_callback
def process(frames):
    # Audio thread: do the absolute minimum, never block.
    for offset, data in in_port.incoming_midi_events():
        try:
            event_q.put_nowait(bytes(data))
        except queue.Full:
            # Drop events rather than blocking the audio thread
            pass

def decode_mido(event_bytes: bytes):
    """Decode raw MIDI bytes into a mido Message if possible."""
    try:
        return mido.Message.from_bytes(event_bytes)
    except ValueError:
        return None

def main():
    client.activate()
    print(f"Started JACK client: {client.name}")
    print(f"Listening on: {client.name}:input")

    # Try to auto-connect from TARGET_PORT -> our input port
    try:
        src = client.get_port_by_name(TARGET_PORT)
        if src is None:
            print(f"Warning: Could not find '{TARGET_PORT}'. Connect manually, e.g.:")
            print(f"  jack_connect {TARGET_PORT} {client.name}:input")
        else:
            # connect(source, destination)
            client.connect(src, in_port)
            print(f"Connected {TARGET_PORT} -> {client.name}:input")
    except jack.JackError as e:
        print(f"Connection error: {e}")
        print(f"You can connect manually with:")
        print(f"  jack_connect {TARGET_PORT} {client.name}:input")

    print("Listening for MIDI events... (Ctrl+C to stop)")

    last_prog = None

    try:
        while True:
            try:
                data = event_q.get(timeout=1.0)
            except queue.Empty:
                continue

            msg = decode_mido(data)

            # Debug print (safe here; not in audio thread)
            if msg is not None:
                print(f"Received: {msg!r}")
            else:
                print(f"Received raw bytes: {list(data)}")

            if msg is None or msg.type != "program_change":
                continue

            if FILTER_CHANNEL is not None and msg.channel != FILTER_CHANNEL:
                continue

            prog = msg.program

            # Optional debounce (some controllers spam PC)
            if prog == last_prog:
                continue
            last_prog = prog

            print(f"🎹 PROGRAM CHANGE -> program={prog}, channel={msg.channel}")

            cmds = PROGRAM_MAP.get(prog)
            if not cmds:
                print("⚠️  No mapping for this program")
                continue

            for cmd in cmds:
                print(f"→ mod-host: {cmd}")
                #try:
                #    send_modhost(cmd)
                #except OSError as e:
                #    print(f"⚠️  mod-host send failed: {e}")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            client.deactivate()
        finally:
            client.close()

if __name__ == "__main__":
    main()
