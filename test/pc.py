#!/usr/bin/env python3
import jack
import time

# ===== CONFIG =====
TARGET_PORT = "system:midi_playback_1"
PROGRAM = 14          # 0–127  (14 = P015)
CHANNEL = 1           # Human MIDI channel (1–16)
# ==================

client = jack.Client("PC_Sender")
outport = client.midi_outports.register("out")

@client.set_process_callback
def process(frames):
    # Send exactly ONE Program Change, then disconnect
    status = 0xC0 | (CHANNEL - 1)
    event = bytes([status, PROGRAM])
    outport.write_midi_event(0, event)

    # Stop JACK callback after sending
    client.deactivate()

client.activate()

print(f"Sending Program Change {PROGRAM} (P{PROGRAM+1:03d}) on channel {CHANNEL}")
print(f"Connecting to {TARGET_PORT}")

# Connect JACK ports
target = client.get_port_by_name(TARGET_PORT)
client.connect(outport, target)

# Give JACK one cycle to deliver the event
time.sleep(0.1)

client.close()
print("Done.")
