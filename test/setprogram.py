#!/usr/bin/env python3
import rtmidi
import time
import sys

PROGRAM = 14          # MIDI program number (0–127). 14 = P015
COMMON_CHANNEL = 1    # SL88 Common Channel (human 1–16)

def open_sl_ctrl_out():
    """
    Find and open the SL CTRL ALSA MIDI output port.

    Returns (midi_out, port_name).
    """
    midiout = rtmidi.MidiOut()
    ports = midiout.get_ports()

    if not ports:
        raise RuntimeError("No ALSA MIDI OUT ports found.")

    for idx, name in enumerate(ports):
        if "SL CTRL" in name:
            midiout.open_port(idx)
            log(f"[sfizz-router] Opened MIDI OUT: {name} (index {idx})")
            return midiout, name

    for idx, name in enumerate(ports):
        if "SL" in name:
            midiout.open_port(idx)
            log(f"[sfizz-router] Opened MIDI OUT (fallback): {name} (index {idx})")
            return midiout, name

    raise RuntimeError("Could not find an SL CTRL or SL* MIDI OUT port.")

def force_initial_program(midiout, program: int) -> None:
    """
    Send a Program Change on the COMMON channel to force the SL88
    to the given program (e.g. 10 -> P011).
    """
    common_midi_channel = COMMON_CHANNEL - 1  # 16 -> 15
    status = 0xC0 | common_midi_channel       # Program Change on that channel
    msg = [status, program]
    midiout.send_message(msg)
    log(f"[sfizz-router] Forced SL88 to program={program} (P{program+1:03d}) on ch={COMMON_CHANNEL}")


def main():
    try:
        midiout, out_name = open_sl_ctrl_out()
    except Exception as e:
        print(f"[sfizz-router] ERROR opening MIDI ports: {e}")
        sys.exit(1)

    force_initial_program(midiout,15)


if __name__ == "__main__":
    main()
