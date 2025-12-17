#!/usr/bin/env python3
import jack
import time
import threading

TARGET_PORT = "system:midi_playback_1"
PROGRAM = 14   # 0-127
CHANNEL = 2    # 1-16

done = threading.Event()
sent = False

client = jack.Client("PC_Sender")
outport = client.midi_outports.register("out")

@client.set_process_callback
def process(frames):
    global sent
    if sent:
        return
    status = 0xC0 | (CHANNEL - 1)
    outport.write_midi_event(0, bytes([status, PROGRAM]))
    sent = True
    done.set()

# Activate
client.activate()

# Connect
target = client.get_port_by_name(TARGET_PORT)
client.connect(outport, target)

print(f"Connected {client.name}:out -> {TARGET_PORT}")
print(f"Sending PC program={PROGRAM} (P{PROGRAM+1:03d}) ch={CHANNEL}")

done.wait(2.0)
time.sleep(0.1)

client.deactivate()
client.close()
print("Done.")
