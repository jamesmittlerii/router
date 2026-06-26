"""Launch Control XL 3 custom-mode SysEx helpers."""

from __future__ import annotations

NOVATION_LCXL3_HEADER = bytes([0xF0, 0x00, 0x20, 0x29, 0x02, 0x15])
WRITE_CMD = bytes([0x05, 0x00, 0x45])

# Control slot IDs (from Components / .syx dumps).
ENCODER_BASE = 0x10  # 24 encoders: 0x10 .. 0x27
FADER_BASE = 0x28  # 8 faders: 0x28 .. 0x2f
BUTTON_BASE = 0x30  # 16 fader buttons: 0x30 .. 0x3f

# Pages used by Novation Components exports.
PAGE_ENCODERS = 0x00
PAGE_FADERS_BUTTONS = 0x03

# Label record markers (NOT g/h/m — those were wrong).
LABEL_MARKER_ENCODER = 0x7A
LABEL_MARKER_FADER = 0x7F
LABEL_MARKER_BUTTON = 0x60

ENCODER_LABEL_WIDTH = 26
FADER_LABEL_WIDTH = 31

# Encoder row tag in I-block byte 3 (Components export).
_ENCODER_ROW_TAG = (0x05, 0x09, 0x0D)

LIVE_CUSTOM_MODE_SLOT = 0x7F
SLOT_SELECT_HEADER = bytes([0xF0, 0x00, 0x20, 0x29, 0x02, 0x77])

# Factory Custom Mode 1 layout (Novation Components blank template / classic LCXL).
DEFAULT_FADER_CCS: tuple[int, ...] = tuple(range(13, 21))
DEFAULT_ENCODER_ROW_CCS: tuple[tuple[int, ...], ...] = (
    tuple(range(21, 29)),
    tuple(range(41, 49)),
    tuple(range(57, 65)),
)
DEFAULT_ENCODER_CCS: tuple[int, ...] = tuple(
    cc for row in DEFAULT_ENCODER_ROW_CCS for cc in row
)

# Feature-control port uses MIDI channel 7 (CC) and 16 (Note) per Novation docs.
_DAW_FEATURE_CC_MIDO_CHANNEL = 6
_DAW_FEATURE_NOTE_MIDO_CHANNEL = 15


def build_slot_select_sysex(slot: int) -> bytes:
    """Select custom-mode slot on the hardware (0-based)."""
    return SLOT_SELECT_HEADER + bytes([slot & 0x7F, 0xF7])


def build_global_channel_midi_messages(channel: int) -> list[bytes]:
    """
    Set LCXL3 custom-mode global MIDI channel via DAW-In feature controls.

    Per Novation programmer reference: enable feature controls (Note 11 ch16),
    then CC 100 on channel 7 with value 0-15 (0 = MIDI channel 1).
    """
    ch_val = _midi_channel_byte(channel)
    return [
        bytes([0x90 | _DAW_FEATURE_NOTE_MIDO_CHANNEL, 0x0B, 0x7F]),
        bytes([0xB0 | _DAW_FEATURE_CC_MIDO_CHANNEL, 0x64, ch_val]),
    ]


def _write_open(page: int, slot: int, name: str) -> bytearray:
    name_bytes = name.encode("ascii", errors="replace")[:14]
    return bytearray(
        NOVATION_LCXL3_HEADER + WRITE_CMD + bytes([page & 0x7F, slot & 0x7F, 0x20, len(name_bytes)])
    ) + name_bytes


def _pad_label(text: str, width: int) -> bytes:
    raw = text.encode("ascii", errors="replace")[:width]
    return raw + b"\x00" * (width - len(raw))


def _midi_channel_byte(channel: int) -> int:
    """Components stores MIDI channel as 0-based in I-block byte 7."""
    return max(0, min(15, int(channel) - 1)) & 0x7F


def build_encoder_block(slot_id: int, cc: int, *, row: int, channel: int = 1) -> bytes:
    """Encoder CC assignment (Components export layout)."""
    row_tag = _ENCODER_ROW_TAG[row] if 0 <= row < len(_ENCODER_ROW_TAG) else _ENCODER_ROW_TAG[0]
    return bytes(
        [
            0x49,
            slot_id & 0x7F,
            0x02,
            row_tag,
            0x00,
            0x01,
            0x40,
            _midi_channel_byte(channel),
            cc & 0x7F,
            0x7F,
            0x00,
        ]
    )


def build_fader_block(slot_id: int, cc: int, *, channel: int = 1) -> bytes:
    """Fader CC assignment (Components export layout)."""
    return bytes(
        [
            0x49,
            slot_id & 0x7F,
            0x02,
            0x00,
            0x00,
            0x01,
            0x40,
            _midi_channel_byte(channel),
            cc & 0x7F,
            0x7F,
            0x00,
        ]
    )


def build_button_block(slot_id: int, cc: int, *, channel: int = 1) -> bytes:
    """Fader-button CC assignment (Components export layout)."""
    group = 0x19 if slot_id < 0x38 else 0x25
    return bytes(
        [
            0x49,
            slot_id & 0x7F,
            0x02,
            group,
            0x03,
            0x01,
            0x50,
            _midi_channel_byte(channel),
            cc & 0x7F,
            0x7F,
            0x00,
        ]
    )


def build_button_unassigned_block(slot_id: int, *, channel: int = 1) -> bytes:
    """Placeholder button block matching Components defaults."""
    default_cc = 0x25 + (slot_id - BUTTON_BASE)
    return build_button_block(slot_id, default_cc, channel=channel)


def build_encoder_label(slot_id: int, text: str) -> bytes:
    """Fixed-width encoder label record (28 bytes)."""
    return bytes([LABEL_MARKER_ENCODER, slot_id & 0x7F]) + _pad_label(text, ENCODER_LABEL_WIDTH)


def build_fader_label(slot_id: int, text: str) -> bytes:
    """Fixed-width fader label record (33 bytes)."""
    return bytes([LABEL_MARKER_FADER, slot_id & 0x7F]) + _pad_label(text, FADER_LABEL_WIDTH)


def build_button_label_marker(slot_id: int) -> bytes:
    """Button label placeholder (2 bytes); Components uses this after button I-blocks."""
    return bytes([LABEL_MARKER_BUTTON, slot_id & 0x7F])


def build_encoder_page_sysex(
    name: str,
    encoders: list[int],
    labels: dict[int, str] | None = None,
    *,
    channel: int = 1,
    slot: int = LIVE_CUSTOM_MODE_SLOT,
) -> bytes:
    body = _write_open(PAGE_ENCODERS, slot, name)
    labeled: list[tuple[int, int, str]] = []
    unlabeled_slots: list[int] = []
    for idx, cc in enumerate(encoders[:24]):
        slot_id = ENCODER_BASE + idx
        row = idx // 8
        body.extend(build_encoder_block(slot_id, cc, row=row, channel=channel))
        label = (labels or {}).get(cc, "")
        if label:
            labeled.append((slot_id, cc, label))
        else:
            unlabeled_slots.append(slot_id)
    for slot_id, _cc, label in labeled:
        body.extend(build_encoder_label(slot_id, label))
    for slot_id in unlabeled_slots:
        body.extend(build_button_label_marker(slot_id))
    body.append(0xF7)
    return bytes(body)


def build_fader_button_page_sysex(
    name: str,
    faders: list[int],
    buttons: list[int | None],
    labels: dict[int, str] | None = None,
    *,
    channel: int = 1,
    slot: int = LIVE_CUSTOM_MODE_SLOT,
) -> bytes:
    """
    Page 3 layout (from Components .syx exports):

    - 8 contiguous fader I-blocks
    - 8 fixed-width fader label records (0x7F marker)
    - 16 button I-blocks
    - 16 button label markers (0x60 marker)
    """
    body = _write_open(PAGE_FADERS_BUTTONS, slot, name)
    fader_slots: list[int] = []
    for idx, cc in enumerate(faders[:8]):
        slot_id = FADER_BASE + idx
        fader_slots.append(slot_id)
        body.extend(build_fader_block(slot_id, cc, channel=channel))
    for idx, cc in enumerate(faders[:8]):
        label = (labels or {}).get(cc, "")
        body.extend(build_fader_label(fader_slots[idx], label))
    padded_buttons: list[int | None] = list(buttons[:16])
    while len(padded_buttons) < 16:
        padded_buttons.append(None)
    for idx, cc in enumerate(padded_buttons):
        slot_id = BUTTON_BASE + idx
        if cc is None:
            body.extend(build_button_unassigned_block(slot_id, channel=channel))
        else:
            body.extend(build_button_block(slot_id, cc, channel=channel))
    for idx in range(16):
        body.extend(build_button_label_marker(BUTTON_BASE + idx))
    body.append(0xF7)
    return bytes(body)


def build_custom_mode_sysex(
    name: str,
    *,
    faders: list[int],
    encoders: list[list[int]],
    buttons: list[int | None] | None = None,
    channel: int = 1,
    slot: int = LIVE_CUSTOM_MODE_SLOT,
    labels: dict[int, str] | None = None,
) -> bytes:
    """
    Build a single concatenated upload (legacy).

    Prefer build_custom_mode_messages() which matches Components exports.
    The channel argument is kept for API compatibility; LCXL3 uses the
    custom mode Global Channel setting on the hardware for MIDI output.
    """
    _ = channel
    flat_encoders = [cc for row in encoders for cc in row]
    msgs = build_custom_mode_messages(
        name,
        faders=faders,
        encoders=flat_encoders,
        buttons=buttons or [],
        channel=channel,
        slot=slot,
        labels=labels,
    )
    return b"".join(msgs)


def build_custom_mode_messages(
    name: str,
    *,
    faders: list[int],
    encoders: list[int],
    buttons: list[int | None] | None = None,
    channel: int = 1,
    slot: int = LIVE_CUSTOM_MODE_SLOT,
    labels: dict[int, str] | None = None,
) -> list[bytes]:
    """Build paged SysEx messages like Novation Components .syx exports."""
    short_name = name[:14]
    messages: list[bytes] = []
    has_fader_content = bool(faders) or any(cc is not None for cc in (buttons or []))
    # Page 0 carries the custom-mode name on the LCD; always send it on instrument change.
    messages.append(
        build_encoder_page_sysex(
            short_name, encoders, labels=labels, channel=channel, slot=slot
        )
    )
    if has_fader_content:
        messages.append(
            build_fader_button_page_sysex(
                short_name,
                faders,
                buttons or [],
                labels=labels,
                channel=channel,
                slot=slot,
            )
        )
    return messages


def default_lcxl_cc_lists() -> tuple[list[int], list[int], list[int | None]]:
    """Faders, flat encoders, and buttons for the factory Custom Mode 1 CC map."""
    return list(DEFAULT_FADER_CCS), list(DEFAULT_ENCODER_CCS), []


def resolve_lcxl_cc_lists(
    raw: object,
) -> tuple[list[int], list[int], list[int | None]]:
    """
    Resolve fader/encoder/button CC lists for upload.

    Missing ``lcxl`` section -> full factory defaults. Partial sections (e.g.
    faders only) fill unspecified control groups from the same defaults.
    """
    layout = parse_lcxl_layout(raw)
    if layout is None:
        return default_lcxl_cc_lists()

    default_faders, default_encoders, default_buttons = default_lcxl_cc_lists()
    faders = layout["faders"] if layout["faders"] else list(default_faders)
    flat_encoders = [cc for row in layout["encoders"] for cc in row]
    if not flat_encoders:
        flat_encoders = list(default_encoders)
    buttons = layout["buttons"] if layout["buttons"] else list(default_buttons)
    return faders, flat_encoders, buttons


def parse_lcxl_layout(raw: object) -> dict[str, list[int | None]] | None:
    """Parse plugin lcxl section into fader/encoder/button CC lists."""
    if not isinstance(raw, dict):
        return None

    faders_raw = raw.get("fader") or raw.get("faders")
    if not isinstance(faders_raw, list):
        return None

    enc_raw = raw.get("encoder") or raw.get("encoders") or []
    encoders: list[list[int]] = []
    if isinstance(enc_raw, list):
        for row in enc_raw:
            if not isinstance(row, list):
                return None
            encoders.append([int(c) for c in row])

    buttons_raw = raw.get("button") or raw.get("buttons") or []
    buttons: list[int | None] = []
    if isinstance(buttons_raw, list):
        for entry in buttons_raw:
            buttons.append(None if entry is None else int(entry))

    return {
        "faders": [int(c) for c in faders_raw],
        "encoders": encoders,
        "buttons": buttons,
    }
