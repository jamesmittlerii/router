# Launch Control XL3 — custom mode SysEx notes

Reverse-engineered from **Novation Components** `.syx` exports and validated on hardware with `lcxl3.py`, `test/lcxl3_setup_plugin.py`, and `load_single.py`.

Public Novation documentation is **incomplete** for custom-mode writes. Components is the reference implementation. The programmer reference is accurate for **DAW-In feature controls** (global MIDI channel), but not sufficient to build a working uploader from prose alone.

## Quick recipe

1. Build **two** SysEx messages (page 0 + page 3), matching Components layout.
2. Write to live slot **`0x7F`** (`LIVE_CUSTOM_MODE_SLOT`) for runtime updates.
3. Always send **page 0** when switching instruments — the LCD mode name comes from that page.
4. Send SysEx to **DAW-In** (not USB Main).
5. After SysEx, send **global channel** setup on the same DAW-In port (see below).
6. On the hardware: Custom Mode → Edit → enable **USB Main** for MIDI out to the host.

## Message structure

### Headers

| Constant | Bytes |
|----------|-------|
| Write header | `F0 00 20 29 02 15 05 00 45` |
| Slot select | `F0 00 20 29 02 77` + slot + `F7` |

### Write open (both pages)

```
F0 00 20 29 02 15 05 00 45  <page>  <slot>  20  <name_len>  <name ascii, max 14>  ...body...  F7
```

| Field | Value |
|-------|-------|
| Page | `0x00` = encoders, `0x03` = faders + buttons |
| Slot | `0x7F` = live/active mode; `0x00`–`0x0E` = stored custom mode slots |

### Control slots

| Control | Slot IDs |
|---------|----------|
| Encoders (24) | `0x10` … `0x27` |
| Faders (8) | `0x28` … `0x2F` |
| Fader buttons (16) | `0x30` … `0x3F` |

## I-block (`0x49`) — 11 bytes

```
49  <slot>  02  <b3>  <b4>  <b5>  <b6>  <b7>  <cc>  7F  00
```

| Byte | Encoders / faders | Buttons |
|------|-------------------|---------|
| 3 | Row tag: `0x05`, `0x09`, `0x0D` (rows 1–3) | Group: `0x19` or `0x25` |
| 4 | `0x00` (enc/fader) | `0x03` |
| 5 | `0x01` | `0x01` |
| 6 | **`0x40`** | **`0x50`** |
| 7 | 0-based MIDI channel | 0-based MIDI channel |
| 8 | **CC number** | **CC number** |

**Do not** put CC in byte 3. That was a common wrong assumption.

## Label records

Labels are **not** `g` / `h` / `m` prefixed ASCII (LCXL1/2 folklore does not apply).

| Control | Marker | Text width | Record size |
|---------|--------|------------|-------------|
| Encoder | `0x7A` + slot | 26 bytes, null-padded | 28 bytes |
| Fader | `0x7F` + slot | 31 bytes, null-padded | 33 bytes |
| Button | `0x60` + slot | (placeholder, no text) | 2 bytes |

## Page 0 (encoders)

1. Write open with mode name.
2. Up to 24 encoder I-blocks (in slot order).
3. Label records for encoders that have text; `0x60` markers for unlabeled encoder slots.
4. `F7`

Even fader-only instruments (e.g. Connie drawbars) need a page 0 upload to update the **mode name** on the LCD.

## Page 3 (faders + buttons)

**Order matters.** Do not interleave labels between fader I-blocks.

1. Write open with mode name.
2. **All 8** fader I-blocks (contiguous).
3. **All 8** fader label records (`0x7F`).
4. **All 16** button I-blocks (assigned or placeholder).
5. **All 16** button label markers (`0x60`).
6. `F7`

Interleaving fader labels between I-blocks breaks CC assignment (e.g. every fader ends up on the same CC).

## Global MIDI channel

I-block byte 7 stores a 0-based channel in the blob, but **hardware output did not follow byte 7 alone** in our testing. Set the live channel via DAW-In feature controls **after** SysEx:

| Step | MIDI | Purpose |
|------|------|---------|
| 1 | Note 11, vel 127, **channel 16** | Enable feature controls (`9F 0B 7F`) |
| 2 | CC 100, val = channel − 1, **channel 7** | Global MIDI channel (`B6 64 nn`) |

Implemented in `lcxl3.build_global_channel_midi_messages()`.

This matches Novation’s programmer reference for feature controls.

## Wrong assumptions (summary)

| Topic | Wrong | Right |
|-------|-------|-------|
| CC in I-block | Byte 3 | **Byte 8** |
| I-block byte 6 | Same everywhere | `0x40` enc/fader, `0x50` button |
| Encoder byte 3 | CC-related | **Row tag** |
| Labels | `g` / `h` / `m` strings | **`0x7A` / `0x7F` / `0x60`** + fixed-width padding |
| Page 3 layout | Interleaved labels | **Blocks first, then labels** |
| Upload | Single SysEx | **Page 0 + page 3** |
| MIDI channel | SysEx byte 7 only | **DAW-In feature MIDI** after upload |
| Mode name | Any page | **Page 0** write header |
| Live updates | Stored slot 0 | Slot **`0x7F`** |

## Router integration

Pedalboard plugins can define an `lcxl` section in JSON (`jsons/plus.json`). On program change, `load_single.configure_lcxl_for_plugin()` uploads the matching layout via JACK → LCXL DAW-In.

| Instrument | Example | CC handling |
|------------|---------|-------------|
| SFZ (Caveman) | Plugin 26 | `midi_cc` with `pass_through: true` → MIDI to sfizz on **ch 2** |
| Connie | Plugin 28 | `midi_cc` string map → `param_set` on LV2 ports (`db_16`, etc.) |

All LCXL CC traffic uses **`COMMON_CHANNEL` (2)** — same as SL88 program change and SFZ pass-through.

### Plugin JSON shape

```json
"lcxl": {
  "fader": [5, 6, 7, 8, 9, 10],
  "encoder": [[25, 26, ...], [...], [...]],
  "button": [126, 127]
},
"midi_cc": {
  "5": "db_16",
  "15": {"pass_through": true, "default": 64, "label": "Bass Osc Vol"}
}
```

Labels come from `midi_cc` entries (`label`, `param`, `symbol`, or string value).

## Test tools

```bash
# Upload layout for plugin 28 (Connie), listen for CC on USB Main
python test/lcxl3_setup_plugin.py 28 --listen 150

# Parse a Components export
python test/lcxl3_analyze_syx.py path/to/export.syx

# Diff two exports (e.g. different global channels)
python test/lcxl3_diff_syx.py ch1.syx ch2.syx
```

## Code references

| File | Role |
|------|------|
| `lcxl3.py` | SysEx builder |
| `load_single.py` | `configure_lcxl_for_plugin()`, CC routing |
| `test/lcxl3_setup_plugin.py` | Standalone upload + listen (Windows rtmidi / Linux JACK) |
