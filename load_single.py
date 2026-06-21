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
import subprocess
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import jack
import mido

from lcxl3 import build_custom_mode_messages, build_global_channel_midi_messages, parse_lcxl_layout

# ---- Configuration ----

MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))
COMMON_CHANNEL = 2  # User confirmed Channel 2
FLUIDA_URI = "https://github.com/brummer10/Fluida.lv2"
PIANO_PLUGIN_URIS = frozenset(
    {
        "http://sfztools.github.io/sfizz",
        FLUIDA_URI,
        "http://studionumbersix.com/foo/lv2/yc20",
        "http://bristol.sourceforge.net/lv2/vox",
        "https://ho-ro.net/connie/lv2",
    }
)
SL88_TARGET_PORT = "system:midi_playback_1"
FLUIDA_PRESET_CHANNEL = int(os.environ.get("FLUIDA_PRESET_CHANNEL", "1"))  # 1-16, note channel
FLUIDA_PRESET_CC = int(os.environ.get("FLUIDA_PRESET_CC", "80"))  # CC on COMMON_CHANNEL -> preset
KILL_PC = 50 # set this to a PC to force a shutdown
OUTPUT_GAIN_URI = "http://moddevices.com/plugins/mod-devel/Gain2x2"
OUTPUT_GAIN_PARAM = "Gain"

# Configuration for State Persistence
STATE_FILE = Path(os.environ.get("ROUTER_STATE", "/var/lib/router/last_state.json"))
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Which JACK MIDI source to tap for Program Changes
TARGET_PORT = "system:midi_capture_1"
FILTER_CHANNEL = None  # Set to 0-15 to filter by channel, or None for all

# Launch Control XL3 drawbar CC (1-based MIDI channel, matches controller display)
CC_TARGET_PORT = os.environ.get("CC_TARGET_PORT", "system:midi_capture_4")
CC_CHANNEL = int(os.environ.get("CC_CHANNEL", str(COMMON_CHANNEL)))  # 1-16
CC_SOFT_TAKEOVER = os.environ.get("CC_SOFT_TAKEOVER", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CC_PICKUP_THRESHOLD = max(0, min(127, int(os.environ.get("CC_PICKUP_THRESHOLD", "1"))))
CC_STATE_SAVE_DEBOUNCE_S = float(os.environ.get("CC_STATE_SAVE_DEBOUNCE_S", "0.5"))
LCXL_DAW_IN_PORT = os.environ.get("LCXL_DAW_IN_PORT", "")
LCXL_CUSTOM_MODE_SLOT = int(os.environ.get("LCXL_CUSTOM_MODE_SLOT", "0"))

_state_save_lock = threading.Lock()
_last_state_write_mono = 0.0


stop_event = threading.Event()
_shutdown_signal: Optional[int] = None
_log_lock = threading.Lock()


def request_stop(signum, frame):
    # Only set flags here — print() is not async-signal-safe and interleaves
    # badly with main-thread output under systemd/journald.
    global _shutdown_signal
    _shutdown_signal = signum
    stop_event.set()


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)   # Ctrl-C too


def configure_stdio() -> None:
    """Line-buffer stdout/stderr so journald receives one record per line."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True, write_through=True)
        except TypeError:
            try:
                reconfigure(line_buffering=True)
            except (OSError, ValueError):
                pass
        except (OSError, ValueError):
            pass


def log(msg: str) -> None:
    """Write one line to stdout; one os.write() per call for live journalctl -f."""
    with _log_lock:
        try:
            os.write(1, (msg + "\n").encode("utf-8", errors="replace"))
        except OSError:
            print(msg, flush=True)


configure_stdio()


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


def mod_disconnect_quiet(src: str, dst: str) -> None:
    try:
        send_cmd(f'disconnect "{src}" "{dst}"')
    except Exception as e:
        print(f"Failed to disconnect {src}->{dst}: {e}")


def mod_remove_quiet(instance_id: int) -> None:
    try:
        send_cmd(f"remove {instance_id}")
    except Exception as e:
        print(f"Failed to remove plugin {instance_id}: {e}")


def get_plugin_gain(p: dict[str, Any], fallback: float) -> float:
    """
    Read optional top-level plugin gain from JSON.
    """
    raw = p.get("gain", fallback)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class FluidaPresetConfig:
    preset: int
    bank_msb: int = 0
    bank_lsb: int = 0


def parse_fluida_preset(p: dict[str, Any]) -> Optional[FluidaPresetConfig]:
    """Read optional preset/bank for Fluida SF2 preset selection."""
    if "preset" not in p:
        return None
    try:
        preset = int(p["preset"])
    except (TypeError, ValueError):
        print(f"[Fluida] Warning: invalid preset value {p.get('preset')!r}, ignoring")
        return None
    preset = max(0, min(127, preset))
    bank_msb = 0
    bank_lsb = 0
    if "bank" in p:
        try:
            bank_msb = max(0, min(127, int(p["bank"])))
        except (TypeError, ValueError):
            print(f"[Fluida] Warning: invalid bank value {p.get('bank')!r}, using 0")
    if "bank_lsb" in p:
        try:
            bank_lsb = max(0, min(127, int(p["bank_lsb"])))
        except (TypeError, ValueError):
            print(f"[Fluida] Warning: invalid bank_lsb value {p.get('bank_lsb')!r}, using 0")
    return FluidaPresetConfig(preset=preset, bank_msb=bank_msb, bank_lsb=bank_lsb)


def build_fluida_preset_messages(
    config: FluidaPresetConfig,
    *,
    channel: int,
) -> list[bytes]:
    """Bank select (CC 0/32) + program change for Fluida preset selection."""
    ch = channel & 0x0F
    return [
        bytes([0xB0 | ch, 0, config.bank_msb]),
        bytes([0xB0 | ch, 32, config.bank_lsb]),
        bytes([0xC0 | ch, config.preset]),
    ]


def fluida_midi_in_port(instance: int) -> str:
    return expand_port(f"{instance}:MIDI_IN")


def sfizz_control_port(instance: int) -> str:
    return expand_port(f"{instance}:control")


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


@dataclass(frozen=True)
class CcParamMapping:
    instance: int
    param: str
    min_val: float = 0.0
    max_val: float = 1.0
    pass_through: bool = False
    default_midi: int = 0


def parse_midi_cc_entry(raw: Any) -> tuple[str, float, float, bool, int]:
    """Parse a midi_cc map value (symbol string or {param, min, max} object)."""
    if isinstance(raw, str):
        return raw, 0.0, 1.0, False, 0
    if isinstance(raw, dict):
        if raw.get("pass_through"):
            label = raw.get("label") or raw.get("param") or raw.get("symbol")
            if not label:
                raise ValueError("pass_through midi_cc entry needs 'label'")
            try:
                default_midi = int(raw.get("default", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid default in midi_cc entry: {raw}") from exc
            return str(label), 0.0, 127.0, True, max(0, min(127, default_midi))
        param = raw.get("param") or raw.get("symbol")
        if not param:
            raise ValueError("midi_cc entry dict needs 'param' or 'symbol'")
        try:
            min_val = float(raw.get("min", 0.0))
            max_val = float(raw.get("max", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid min/max in midi_cc entry: {raw}") from exc
        return str(param), min_val, max_val, False, 0
    raise ValueError(f"unsupported midi_cc entry type: {type(raw).__name__}")


def build_cc_map(plugins: dict[str, Any]) -> dict[int, CcParamMapping]:
    """Build MIDI CC number -> plugin param mapping from plugin midi_cc sections."""
    cc_map: dict[int, CcParamMapping] = {}
    for sid, plugin in plugins.items():
        if not isinstance(plugin, dict):
            continue
        midi_cc = plugin.get("midi_cc")
        if not isinstance(midi_cc, dict) or not midi_cc:
            continue
        inst = int(sid)
        for cc_key, entry in midi_cc.items():
            try:
                cc_num = int(cc_key)
                param, min_val, max_val, pass_through, default_midi = (
                    parse_midi_cc_entry(entry)
                )
            except (TypeError, ValueError) as exc:
                print(f"Warning: skipping midi_cc {sid}[{cc_key!r}]: {exc}")
                continue
            if cc_num in cc_map:
                prev = cc_map[cc_num]
                print(
                    f"Warning: CC {cc_num} remapped {prev.instance}:{prev.param}"
                    f" -> {inst}:{param}"
                )
            if pass_through:
                param = cc_param_key(cc_num)
            cc_map[cc_num] = CcParamMapping(
                inst,
                param,
                min_val,
                max_val,
                pass_through=pass_through,
                default_midi=default_midi,
            )
    return cc_map


def midi_cc_to_param(midi_val: int, mapping: CcParamMapping) -> float:
    """Scale 7-bit MIDI (0-127) to plugin param range."""
    t = max(0, min(127, midi_val)) / 127.0
    return mapping.min_val + (mapping.max_val - mapping.min_val) * t


def param_to_midi(param_val: float, mapping: CcParamMapping) -> int:
    """Scale plugin param to nearest 7-bit MIDI value."""
    span = mapping.max_val - mapping.min_val
    if span <= 0.0:
        return 0
    t = (float(param_val) - mapping.min_val) / span
    return max(0, min(127, round(t * 127.0)))


@dataclass
class CcPickupState:
    armed: bool = False
    last_midi: Optional[int] = None
    target_midi: int = 0


def seed_applied_params(plugins: dict[str, Any]) -> dict[tuple[int, str], float]:
    """Collect startup control values from pedalboard JSON."""
    applied: dict[tuple[int, str], float] = {}
    for sid, plugin in plugins.items():
        if not isinstance(plugin, dict):
            continue
        inst = int(sid)
        controls = plugin.get("controls") or {}
        if isinstance(controls, dict):
            for symbol, val in controls.items():
                try:
                    applied[(inst, str(symbol))] = float(val)
                except (TypeError, ValueError):
                    continue
        midi_cc = plugin.get("midi_cc")
        if not isinstance(midi_cc, dict):
            continue
        for cc_key, entry in midi_cc.items():
            try:
                _, _, _, pass_through, default_midi = parse_midi_cc_entry(entry)
            except (TypeError, ValueError):
                continue
            if not pass_through:
                continue
            try:
                cc_num = int(cc_key)
            except (TypeError, ValueError):
                continue
            applied[(inst, cc_param_key(cc_num))] = float(default_midi)
    return applied


def cc_param_key(cc_num: int) -> str:
    return f"cc_{cc_num}"


def init_cc_pickup(
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
) -> dict[int, CcPickupState]:
    pickup: dict[int, CcPickupState] = {}
    for cc, mapping in cc_map.items():
        param_val = applied_params.get(
            (mapping.instance, mapping.param),
            float(mapping.default_midi) if mapping.pass_through else mapping.min_val,
        )
        pickup[cc] = CcPickupState(
            armed=False,
            target_midi=param_to_midi(param_val, mapping),
        )
    return pickup


def reset_pickup_for_instance(
    instance: int,
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
    cc_pickup: dict[int, CcPickupState],
) -> list[int]:
    """Re-arm soft takeover for CCs mapped to this plugin instance."""
    reset_ccs: list[int] = []
    for cc, mapping in cc_map.items():
        if mapping.instance != instance:
            continue
        param_val = applied_params.get(
            (mapping.instance, mapping.param),
            float(mapping.default_midi) if mapping.pass_through else mapping.min_val,
        )
        cc_pickup[cc] = CcPickupState(
            armed=False,
            target_midi=param_to_midi(param_val, mapping),
        )
        reset_ccs.append(cc)
    return reset_ccs


def pickup_should_apply(
    state: CcPickupState,
    midi_val: int,
    threshold: int = CC_PICKUP_THRESHOLD,
) -> bool:
    """Return True when the fader has caught the current param value."""
    if state.armed:
        return True

    target = state.target_midi
    if abs(midi_val - target) <= threshold:
        return True

    prev = state.last_midi
    if prev is not None and (
        (prev <= target <= midi_val) or (midi_val <= target <= prev)
    ):
        return True

    return False


def instances_with_cc_maps(cc_map: dict[int, CcParamMapping]) -> set[int]:
    return {mapping.instance for mapping in cc_map.values()}


def mapped_param_keys(cc_map: dict[int, CcParamMapping]) -> set[tuple[int, str]]:
    return {(mapping.instance, mapping.param) for mapping in cc_map.values()}


def build_plugin_controls_snapshot(
    applied_params: dict[tuple[int, str], float],
    cc_map: dict[int, CcParamMapping],
) -> dict[str, dict[str, float]]:
    """Snapshot only params referenced by midi_cc mappings."""
    snapshot: dict[str, dict[str, float]] = {}
    for mapping in cc_map.values():
        val = applied_params.get((mapping.instance, mapping.param))
        if val is None:
            continue
        inst_key = str(mapping.instance)
        snapshot.setdefault(inst_key, {})[mapping.param] = val
    return snapshot


def load_router_state() -> tuple[Optional[int], dict[str, dict[str, float]]]:
    if not STATE_FILE.exists():
        return None, {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[State] Warning: Failed to load state file: {e}")
        return None, {}
    restored = data.get("last_active_piano")
    plugin_controls = data.get("plugin_controls", {})
    if not isinstance(plugin_controls, dict):
        plugin_controls = {}
    return restored, plugin_controls


def normalize_restored_piano(
    restored: Any, piano_ids: list[int]
) -> Optional[int]:
    """Validate/coerce saved active piano against the current pedalboard."""
    if restored is None:
        return None
    try:
        inst = int(restored)
    except (TypeError, ValueError):
        print(f"[State] Warning: invalid last_active_piano {restored!r}, ignoring")
        return None
    if inst not in piano_ids:
        print(
            f"[State] Warning: last_active_piano {inst} not in current"
            f" pedalboard pianos {sorted(piano_ids)}, ignoring"
        )
        return None
    return inst


def merge_saved_plugin_controls(
    applied_params: dict[tuple[int, str], float],
    saved_controls: dict[str, Any],
    cc_map: dict[int, CcParamMapping],
) -> list[tuple[int, str, float]]:
    """Overlay saved midi_cc param values onto applied_params. Returns applied triples."""
    mapped = mapped_param_keys(cc_map)
    restored: list[tuple[int, str, float]] = []
    for inst_key, params in saved_controls.items():
        if not isinstance(params, dict):
            continue
        try:
            inst = int(inst_key)
        except (TypeError, ValueError):
            continue
        for symbol, val in params.items():
            sym = str(symbol)
            if (inst, sym) not in mapped:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            applied_params[(inst, sym)] = fval
            restored.append((inst, sym, fval))
    return restored


def save_router_state(
    last_active_piano: Optional[int],
    applied_params: dict[tuple[int, str], float],
    cc_map: dict[int, CcParamMapping],
    *,
    force: bool = False,
) -> None:
    global _last_state_write_mono
    if not cc_map:
        payload: dict[str, Any] = {"last_active_piano": last_active_piano}
    else:
        payload = {
            "last_active_piano": last_active_piano,
            "plugin_controls": build_plugin_controls_snapshot(applied_params, cc_map),
        }

    now = time.monotonic()
    with _state_save_lock:
        if (
            not force
            and CC_STATE_SAVE_DEBOUNCE_S > 0
            and (now - _last_state_write_mono) < CC_STATE_SAVE_DEBOUNCE_S
        ):
            return
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
            _last_state_write_mono = now
        except Exception as e:
            print(f"[State] Failed to save state file: {e}")

# ---- JACK MIDI Handling ----

JACK_CLIENT_NAME = "Router_Loader"
JACK_OPEN_RETRIES = 5
JACK_OPEN_RETRY_DELAY_S = 0.5

event_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)  # Program Change (SL88)
cc_event_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)  # Control Change (LCXL3)
send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=128)   # Outgoing (SL88 sync)
fluida_send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=128)  # Outgoing (Fluida preset)
lcxl_send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=64)  # Outgoing (LCXL3 SysEx)
cc_forward_q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)  # Outgoing (SFZ CC pass-through)


def _enqueue_incoming_midi(
    port: jack.Port,
    target: "queue.Queue[bytes]",
) -> None:
    for _offset, data in port.incoming_midi_events():
        try:
            target.put_nowait(bytes(data))
        except queue.Full:
            pass


def _flush_outgoing_midi(
    out_port: jack.Port,
    source: "queue.Queue[bytes]",
) -> None:
    out_port.clear_buffer()
    while True:
        try:
            msg = source.get_nowait()
            out_port.write_midi_event(0, msg)
        except queue.Empty:
            break


def _jack_process(
    _frames: int,
    inp: jack.Port,
    cc_inp: jack.Port,
    outp: jack.Port,
    preset_outp: jack.Port,
    lcxl_outp: jack.Port,
    cc_forward_outp: jack.Port,
) -> None:
    _enqueue_incoming_midi(inp, event_q)
    _enqueue_incoming_midi(cc_inp, cc_event_q)
    _flush_outgoing_midi(outp, send_q)
    _flush_outgoing_midi(preset_outp, fluida_send_q)
    _flush_outgoing_midi(lcxl_outp, lcxl_send_q)
    _flush_outgoing_midi(cc_forward_outp, cc_forward_q)


def _open_jack_client_once() -> tuple[
    jack.Client,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
]:
    c = jack.Client(JACK_CLIENT_NAME, no_start_server=True)
    inp = c.midi_inports.register("input")
    cc_inp = c.midi_inports.register("cc_input")
    outp = c.midi_outports.register("output")
    preset_outp = c.midi_outports.register("preset_out")
    lcxl_outp = c.midi_outports.register("lcxl_out")
    cc_forward_outp = c.midi_outports.register("cc_forward_out")

    @c.set_process_callback
    def process(frames):
        _jack_process(
            frames,
            inp,
            cc_inp,
            outp,
            preset_outp,
            lcxl_outp,
            cc_forward_outp,
        )

    return c, inp, cc_inp, outp, preset_outp, lcxl_outp, cc_forward_outp


def _log_jack_open_retry(attempt: int, exc: Exception) -> None:
    print(
        f"JACK client '{JACK_CLIENT_NAME}' unavailable"
        f" (attempt {attempt}/{JACK_OPEN_RETRIES}): {exc}"
    )
    time.sleep(JACK_OPEN_RETRY_DELAY_S)


def create_jack_client() -> tuple[
    jack.Client,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
    jack.Port,
]:
    """Open the router JACK MIDI client (retries if a stale client name lingers)."""
    last_err: Optional[Exception] = None
    for attempt in range(1, JACK_OPEN_RETRIES + 1):
        try:
            return _open_jack_client_once()
        except jack.JackOpenError as exc:
            last_err = exc
            if attempt < JACK_OPEN_RETRIES:
                _log_jack_open_retry(attempt, exc)
    raise RuntimeError(
        f"Could not open JACK client '{JACK_CLIENT_NAME}' after"
        f" {JACK_OPEN_RETRIES} attempts: {last_err}"
    ) from last_err


def decode_mido(event_bytes: bytes):
    """Decode raw MIDI bytes into a mido Message if possible."""
    try:
        return mido.Message.from_bytes(event_bytes)
    except ValueError:
        return None


def send_fluida_preset(
    jack_client: jack.Client,
    preset_out: jack.Port,
    instance: int,
    config: FluidaPresetConfig,
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
    *,
    channel: int = FLUIDA_PRESET_CHANNEL,
) -> None:
    """Connect preset_out to Fluida MIDI_IN and queue bank select + program change."""
    dest_name = fluida_midi_in_port(instance)
    dest = jack_client.get_port_by_name(dest_name)
    if dest is None:
        print(f"[Fluida] Warning: port '{dest_name}' not found; preset not sent")
        return

    for src, dst in reversed(jack_midi_connections):
        if src is preset_out:
            try:
                jack_client.disconnect(src, dst)
                jack_midi_connections.remove((src, dst))
            except jack.JackError as e:
                print(f"[Fluida] Failed to disconnect {src.name}->{dst.name}: {e}")

    try:
        jack_client.connect(preset_out, dest)
        jack_midi_connections.append((preset_out, dest))
    except jack.JackError as e:
        print(f"[Fluida] Failed to connect {preset_out.name} -> {dest_name}: {e}")
        return

    mido_ch = channel - 1
    for msg_bytes in build_fluida_preset_messages(config, channel=mido_ch):
        fluida_send_q.put(msg_bytes)
    bank_note = f", bank MSB={config.bank_msb}" if config.bank_msb else ""
    if config.bank_lsb:
        bank_note += f" LSB={config.bank_lsb}"
    print(
        f"[Fluida] Queued preset {config.preset}{bank_note}"
        f" on ch{channel} -> instance {instance}"
    )


def connect_jack_midi_source(
    jack_client: jack.Client,
    port_name: str,
    dest_port: jack.Port,
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
) -> None:
    """Connect a JACK MIDI capture port to one of our input ports."""
    try:
        src_port = jack_client.get_port_by_name(port_name)
        if src_port:
            jack_client.connect(src_port, dest_port)
            jack_midi_connections.append((src_port, dest_port))
            print(f"Connected {port_name} -> {dest_port.name}")
        else:
            print(f"Warning: Could not find '{port_name}'. Connect manually, e.g.:")
            print(f"  jack_connect {port_name} {dest_port.name}")
    except jack.JackError as e:
        print(f"Connection error ({port_name}): {e}")


def find_midi_port_by_alias(
    jack_client: jack.Client, needle: str
) -> Optional[jack.Port]:
    needle_lower = needle.lower()
    for entry in jack_client.get_ports():
        port = (
            jack_client.get_port_by_name(entry)
            if isinstance(entry, str)
            else entry
        )
        if port is None:
            continue
        for alias in port.aliases:
            if needle_lower in alias.lower():
                return port
    return None


def resolve_lcxl_daw_in_port(jack_client: jack.Client) -> Optional[jack.Port]:
    if LCXL_DAW_IN_PORT:
        return jack_client.get_port_by_name(LCXL_DAW_IN_PORT)
    return find_midi_port_by_alias(jack_client, "DAW-In")


def connect_lcxl_daw_in(
    jack_client: jack.Client,
    lcxl_out: jack.Port,
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
) -> None:
    dest = resolve_lcxl_daw_in_port(jack_client)
    if dest is None:
        print(
            "[LCXL3] Warning: DAW-In port not found;"
            " set LCXL_DAW_IN_PORT or connect lcxl_out manually"
        )
        return
    try:
        jack_client.connect(lcxl_out, dest)
        jack_midi_connections.append((lcxl_out, dest))
        print(f"[LCXL3] Connected {lcxl_out.name} -> {dest.name}")
    except jack.JackError as e:
        print(f"[LCXL3] Failed to connect {lcxl_out.name} -> {dest.name}: {e}")


def queue_lcxl_sysex(payload: bytes, label: str) -> None:
    lcxl_send_q.put(payload)
    print(f"[LCXL3] Queued {label} ({len(payload)} bytes)")


def connect_cc_forward_target(
    jack_client: jack.Client,
    cc_forward_out: jack.Port,
    instance: int,
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
) -> None:
    dest_name = sfizz_control_port(instance)
    dest = jack_client.get_port_by_name(dest_name)
    if dest is None:
        print(f"[SFZ CC] Warning: port '{dest_name}' not found; CC not forwarded")
        return

    for src, dst in reversed(jack_midi_connections):
        if src is cc_forward_out:
            try:
                jack_client.disconnect(src, dst)
                jack_midi_connections.remove((src, dst))
            except jack.JackError as e:
                print(
                    f"[SFZ CC] Failed to disconnect {src.name}->{dst.name}: {e}"
                )

    try:
        jack_client.connect(cc_forward_out, dest)
        jack_midi_connections.append((cc_forward_out, dest))
        print(f"[SFZ CC] Connected {cc_forward_out.name} -> {dest_name}")
    except jack.JackError as e:
        print(f"[SFZ CC] Failed to connect {cc_forward_out.name} -> {dest_name}: {e}")


def queue_cc_forward(
    cc_num: int,
    midi_val: int,
    *,
    channel: int = COMMON_CHANNEL,
) -> None:
    status = 0xB0 | (channel - 1)
    cc_forward_q.put(bytes([status, cc_num & 0x7F, midi_val & 0x7F]))


def send_initial_sfz_cc_values(
    instance: int,
    plugin: dict[str, Any],
    applied_params: dict[tuple[int, str], float],
) -> None:
    midi_cc = plugin.get("midi_cc")
    if not isinstance(midi_cc, dict):
        return
    for cc_key, entry in midi_cc.items():
        try:
            cc_num = int(cc_key)
            _, _, _, pass_through, default_midi = parse_midi_cc_entry(entry)
        except (TypeError, ValueError):
            continue
        if not pass_through:
            continue
        saved = applied_params.get((instance, cc_param_key(cc_num)))
        midi_val = int(saved) if saved is not None else default_midi
        midi_val = max(0, min(127, midi_val))
        queue_cc_forward(cc_num, midi_val)
        print(
            f"[SFZ CC] Queued initial CC {cc_num}={midi_val} -> instance {instance}"
        )


def configure_lcxl_for_plugin(
    session: "RouterSession",
    instance: int,
) -> None:
    if session.jack_client is None or session.lcxl_out_port is None:
        return

    plugin = session.plugins.get(str(instance))
    if not isinstance(plugin, dict):
        return

    layout = parse_lcxl_layout(plugin.get("lcxl"))
    if layout is None:
        return

    symbol = plugin.get("symbol") or str(instance)
    name = str(symbol)[:14]
    labels: dict[int, str] = {}
    midi_cc = plugin.get("midi_cc")
    if isinstance(midi_cc, dict):
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
    flat_encoders = [cc for row in layout["encoders"] for cc in row]
    messages = build_custom_mode_messages(
        name,
        faders=layout["faders"],
        encoders=flat_encoders,
        buttons=layout.get("buttons") or [],
        channel=COMMON_CHANNEL,
        slot=LCXL_CUSTOM_MODE_SLOT,
        labels=labels,
    )
    for page_idx, sysex in enumerate(messages):
        queue_lcxl_sysex(sysex, f"custom mode page {page_idx} for {symbol}")
    for idx, msg in enumerate(build_global_channel_midi_messages(COMMON_CHANNEL)):
        queue_lcxl_sysex(msg, f"LCXL3 global MIDI channel {COMMON_CHANNEL} ({idx + 1})")

    uri = plugin.get("uri")
    if uri == "http://sfztools.github.io/sfizz":
        if session.cc_forward_out_port is not None:
            connect_cc_forward_target(
                session.jack_client,
                session.cc_forward_out_port,
                instance,
                session.jack_midi_connections,
            )
        send_initial_sfz_cc_values(instance, plugin, session.applied_params)


def disconnect_jack_midi_connections(
    jack_client: jack.Client,
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
) -> None:
    for src, dst in reversed(jack_midi_connections):
        try:
            jack_client.disconnect(src, dst)
            log(f"Disconnected JACK {src.name} -> {dst.name}")
        except jack.JackError as e:
            print(f"Failed to disconnect JACK {src.name}->{dst.name}: {e}")


def close_jack_client(jack_client: Optional[jack.Client], activated: bool) -> None:
    if jack_client is None:
        return
    if activated:
        try:
            jack_client.deactivate()
        except Exception as e:
            print(f"JACK deactivate failed: {e}")
    try:
        jack_client.close()
    except Exception as e:
        print(f"JACK close failed: {e}")


def sweep_stale_plugins(plugin_ids: list[int]) -> None:
    """Remove leftover mod-host instances from an unclean prior run."""
    if not plugin_ids:
        return
    print("== Sweeping stale mod-host plugins ==")
    for inst in reversed(plugin_ids):
        try:
            resp = send_cmd(f"remove {inst}")
            code = parse_resp(resp)
            if code is not None and code >= 0:
                print(f"  Removed stale plugin {inst}")
        except Exception:
            pass


def cleanup_session(
    *,
    loaded_ids: list[int],
    active_connections: list[tuple[str, str]],
    jack_midi_connections: list[tuple[jack.Port, jack.Port]],
    jack_client: Optional[jack.Client],
    jack_activated: bool,
    persist_state_fn: Optional[Callable[..., None]] = None,
) -> None:
    log("== Cleaning Up Session ==")

    if persist_state_fn is not None:
        try:
            persist_state_fn(force=True)
        except Exception:
            pass

    if jack_client is not None:
        disconnect_jack_midi_connections(jack_client, jack_midi_connections)
        close_jack_client(jack_client, jack_activated)

    for src, dst in reversed(active_connections):
        log(f"Disconnecting {src} -> {dst}")
        mod_disconnect_quiet(src, dst)

    for inst in reversed(loaded_ids):
        log(f"Removing plugin {inst}")
        mod_remove_quiet(inst)


def _pop_cc_event() -> Optional[bytes]:
    try:
        return cc_event_q.get_nowait()
    except queue.Empty:
        return None


def _is_cc_channel_control_change(msg, cc_mido_channel: int) -> bool:
    return (
        msg is not None
        and msg.type == "control_change"
        and msg.channel == cc_mido_channel
    )


def _get_or_create_cc_pickup(
    cc_pickup: dict[int, CcPickupState], control: int
) -> CcPickupState:
    state = cc_pickup.get(control)
    if state is None:
        state = CcPickupState()
        cc_pickup[control] = state
    return state


def _cc_soft_takeover_allows(
    state: CcPickupState, control: int, midi_val: int
) -> bool:
    """Return False when soft takeover blocks applying this CC."""
    if not CC_SOFT_TAKEOVER:
        return True
    if not pickup_should_apply(state, midi_val):
        state.last_midi = midi_val
        return False
    if not state.armed:
        state.armed = True
        print(
            f"🎚️  CC ch{CC_CHANNEL} cc={control} pickup"
            f" (target={state.target_midi}, fader={midi_val})"
        )
    return True


def _apply_cc_to_plugin(
    control: int,
    mapping: CcParamMapping,
    midi_val: int,
    state: CcPickupState,
    applied_params: dict[tuple[int, str], float],
    persist_state: Optional[Callable[..., None]] = None,
) -> None:
    if mapping.pass_through:
        queue_cc_forward(control, midi_val)
        applied_params[(mapping.instance, mapping.param)] = float(midi_val)
        state.target_midi = midi_val
        print(
            f"🎚️  CC ch{CC_CHANNEL} cc={control}"
            f" -> sfizz {mapping.instance} CC {control}={midi_val}"
        )
        if persist_state is not None:
            persist_state()
        return

    param_val = midi_cc_to_param(midi_val, mapping)
    try:
        mod_param_set(mapping.instance, mapping.param, param_val)
    except Exception as e:
        print(
            f"Failed CC ch{CC_CHANNEL} cc={control}"
            f" -> {mapping.instance}:{mapping.param}: {e}"
        )
        return

    applied_params[(mapping.instance, mapping.param)] = param_val
    state.target_midi = midi_val

    print(
        f"🎚️  CC ch{CC_CHANNEL} cc={control}"
        f" -> {mapping.instance}:{mapping.param}={param_val:.3f}"
    )
    if persist_state is not None:
        persist_state()


def _process_cc_event(
    msg,
    *,
    cc_mido_channel: int,
    cc_map: dict[int, CcParamMapping],
    last_seen_midi: dict[int, int],
    cc_pickup: dict[int, CcPickupState],
    applied_params: dict[tuple[int, str], float],
    active_piano: Optional[int],
    persist_state: Optional[Callable[..., None]] = None,
) -> None:
    if not _is_cc_channel_control_change(msg, cc_mido_channel):
        return

    mapping = cc_map.get(msg.control)
    if mapping is None:
        return
    if active_piano is not None and mapping.instance != active_piano:
        return
    if last_seen_midi.get(msg.control) == msg.value:
        return

    last_seen_midi[msg.control] = msg.value
    state = _get_or_create_cc_pickup(cc_pickup, msg.control)
    if not _cc_soft_takeover_allows(state, msg.control, msg.value):
        return

    state.last_midi = msg.value
    _apply_cc_to_plugin(
        msg.control,
        mapping,
        msg.value,
        state,
        applied_params,
        persist_state,
    )


def drain_cc_events(
    last_seen_midi: dict[int, int],
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
    cc_pickup: dict[int, CcPickupState],
    active_piano: Optional[int],
    persist_state: Optional[Callable[..., None]] = None,
) -> None:
    """Apply control changes on CC_CHANNEL via mod-host param_set."""
    if not cc_map:
        return

    cc_mido_channel = CC_CHANNEL - 1
    while True:
        data = _pop_cc_event()
        if data is None:
            break
        _process_cc_event(
            decode_mido(data),
            cc_mido_channel=cc_mido_channel,
            cc_map=cc_map,
            last_seen_midi=last_seen_midi,
            cc_pickup=cc_pickup,
            applied_params=applied_params,
            active_piano=active_piano,
            persist_state=persist_state,
        )


def handle_fluida_preset_cc(
    msg,
    *,
    active_piano: Optional[int],
    fluida_presets: dict[int, FluidaPresetConfig],
    fluida_instance_ids: set[int],
    last_fluida_preset_cc: dict[int, int],
    send_preset: Callable[[int, int], None],
    source: str = "input",
) -> bool:
    """Apply a COMMON_CHANNEL CC as a Fluida preset change. Returns True if handled."""
    if msg.type != "control_change":
        return False
    if msg.control != FLUIDA_PRESET_CC:
        return False

    ch = msg.channel + 1
    print(
        f"[Fluida CC] CC{FLUIDA_PRESET_CC} value={msg.value} on ch{ch}"
        f" ({source})"
    )

    if msg.channel != COMMON_CHANNEL - 1:
        print(f"[Fluida CC] Ignoring: expected ch{COMMON_CHANNEL}")
        return True

    if active_piano is None:
        print("[Fluida CC] Ignoring: no active piano")
        return True

    if active_piano not in fluida_instance_ids:
        print(
            f"[Fluida CC] Ignoring: active instance {active_piano}"
            " is not Fluida"
        )
        return True

    if active_piano not in fluida_presets:
        print(
            f"[Fluida CC] Ignoring: Fluida instance {active_piano}"
            " has no preset config in pedalboard"
        )
        return True

    preset = max(0, min(127, msg.value))
    if last_fluida_preset_cc.get(active_piano) == preset:
        print(
            f"[Fluida CC] Preset {preset} unchanged for instance"
            f" {active_piano}, skipping"
        )
        return True

    last_fluida_preset_cc[active_piano] = preset
    send_preset(active_piano, preset)
    print(
        f"🎹 FLUIDA PRESET CC ch{COMMON_CHANNEL} cc={FLUIDA_PRESET_CC}"
        f" -> instance {active_piano} preset={preset}"
    )
    return True


def drain_fluida_preset_cc_events(
    event_queue: "queue.Queue[bytes]",
    *,
    source: str,
    active_piano: Optional[int],
    fluida_presets: dict[int, FluidaPresetConfig],
    fluida_instance_ids: set[int],
    last_fluida_preset_cc: dict[int, int],
    send_preset: Callable[[int, int], None],
) -> None:
    """Check a JACK MIDI queue for Fluida preset CC messages."""
    while True:
        try:
            data = event_queue.get_nowait()
        except queue.Empty:
            break

        msg = decode_mido(data)
        if msg is None:
            continue

        handle_fluida_preset_cc(
            msg,
            active_piano=active_piano,
            fluida_presets=fluida_presets,
            fluida_instance_ids=fluida_instance_ids,
            last_fluida_preset_cc=last_fluida_preset_cc,
            send_preset=send_preset,
            source=source,
        )

# ---- Main ----

@dataclass
class RouterSession:
    plugins: dict[str, Any]
    connections: list[dict[str, str]]
    cc_map: dict[int, CcParamMapping]
    applied_params: dict[tuple[int, str], float]
    restored_piano: Optional[int]
    cc_mapped_instances: set[int]
    plugin_ids: list[int]
    piano_ids: list[int] = field(default_factory=list)
    active_piano: Optional[int] = None
    program_gains: dict[int, float] = field(default_factory=dict)
    fluida_presets: dict[int, FluidaPresetConfig] = field(default_factory=dict)
    fluida_instance_ids: set[int] = field(default_factory=set)
    output_gain_instance: Optional[int] = None
    output_gain_default: float = 0.0
    loaded_ids: list[int] = field(default_factory=list)
    active_connections: list[tuple[str, str]] = field(default_factory=list)
    jack_midi_connections: list[tuple[jack.Port, jack.Port]] = field(
        default_factory=list
    )
    jack_client: Optional[jack.Client] = None
    jack_activated: bool = False
    in_port: Optional[jack.Port] = None
    cc_in_port: Optional[jack.Port] = None
    out_port: Optional[jack.Port] = None
    preset_out_port: Optional[jack.Port] = None
    lcxl_out_port: Optional[jack.Port] = None
    cc_forward_out_port: Optional[jack.Port] = None


@dataclass
class MidiListenerState:
    last_prog: Optional[int] = None
    last_seen_midi: dict[int, int] = field(default_factory=dict)
    last_fluida_preset_cc: dict[int, int] = field(default_factory=dict)


def load_pedalboard_from_argv() -> dict[str, Any]:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/pedalboard.json", file=sys.stderr)
        sys.exit(2)

    pb_path = Path(sys.argv[1])
    try:
        return json.loads(pb_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Error: File not found: {pb_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {pb_path}: {e}")
        sys.exit(1)


def init_router_session(pb: dict[str, Any]) -> tuple[RouterSession, list[tuple[int, str, float]]]:
    plugins: dict[str, Any] = pb.get("plugins", {})
    connections: list[dict[str, str]] = pb.get("connections", [])
    cc_map = build_cc_map(plugins)
    applied_params = seed_applied_params(plugins)
    restored_piano, saved_plugin_controls = load_router_state()
    restored_cc = merge_saved_plugin_controls(
        applied_params, saved_plugin_controls, cc_map
    )
    session = RouterSession(
        plugins=plugins,
        connections=connections,
        cc_map=cc_map,
        applied_params=applied_params,
        restored_piano=restored_piano,
        cc_mapped_instances=instances_with_cc_maps(cc_map),
        plugin_ids=sorted(int(sid) for sid in plugins.keys()),
    )
    return session, restored_cc


def find_output_gain_config(plugins: dict[str, Any]) -> tuple[Optional[int], float]:
    for sid, plugin in plugins.items():
        if not isinstance(plugin, dict):
            continue
        if plugin.get("uri") != OUTPUT_GAIN_URI:
            continue

        default = 0.0
        out_controls = plugin.get("controls", {})
        if isinstance(out_controls, dict):
            try:
                default = float(out_controls.get(OUTPUT_GAIN_PARAM, 0.0))
            except (TypeError, ValueError):
                default = 0.0
        return int(sid), default
    return None, 0.0


def load_pedalboard_plugins(session: RouterSession) -> None:
    for sid in sorted(session.plugins.keys(), key=lambda x: int(x)):
        p = session.plugins[sid]
        uri = p["uri"]
        inst = int(sid)
        if uri in PIANO_PLUGIN_URIS:
            session.piano_ids.append(inst)
            session.program_gains[inst] = get_plugin_gain(
                p, session.output_gain_default
            )

        if uri == FLUIDA_URI:
            session.fluida_instance_ids.add(inst)
            preset_cfg = parse_fluida_preset(p)
            if preset_cfg is not None:
                session.fluida_presets[inst] = preset_cfg

        print(f"== add {inst} {uri}")
        try:
            mod_add(uri, inst)
            session.loaded_ids.append(inst)
        except Exception as e:
            print(f"Failed to add plugin {inst}: {e}")


def _apply_plugin_patch_state(inst: int, state: dict[str, Any]) -> None:
    for key, val in state.items():
        print(f"== patch_set {inst} {key} = {val}")
        try:
            mod_patch_set(inst, key, str(val))
        except Exception as e:
            print(f"Failed patch_set {inst} {key}: {e}")


def _apply_plugin_controls(
    inst: int,
    controls: dict[str, Any],
    applied_params: dict[tuple[int, str], float],
) -> None:
    for symbol, val in controls.items():
        sym = str(symbol)
        effective = applied_params.get((inst, sym), val)
        print(f"== param_set {inst} {sym} {effective}")
        try:
            mod_param_set(inst, sym, effective)
            try:
                applied_params[(inst, sym)] = float(effective)
            except (TypeError, ValueError):
                pass
        except Exception as e:
            print(f"Failed param_set {inst} {sym}: {e}")


def _resolve_bypass_for_piano(
    inst: int,
    bypass_on: bool,
    restored_piano: Optional[int],
    piano_ids: list[int],
) -> bool:
    if restored_piano is None or inst not in piano_ids:
        return bypass_on
    return inst != restored_piano


def _apply_plugin_bypass(
    inst: int,
    bypass_on: bool,
    piano_ids: list[int],
) -> bool:
    """Apply bypass and return True if this instance became the active piano."""
    print(f"== bypass {inst} {1 if bypass_on else 0}")
    try:
        mod_bypass(inst, bypass_on)
    except Exception as e:
        print(f"Failed bypass {inst}: {e}")
        return False
    return not bypass_on and inst in piano_ids


def apply_pedalboard_state(session: RouterSession) -> None:
    print("== Applying State & Controls ==")
    for sid in sorted(session.plugins.keys(), key=lambda x: int(x)):
        p = session.plugins[sid]
        inst = int(sid)

        state = p.get("state", {}) or {}
        _apply_plugin_patch_state(inst, state)

        controls = p.get("controls", {}) or {}
        _apply_plugin_controls(inst, controls, session.applied_params)

        if "bypass" not in p:
            continue

        bypass_on = _resolve_bypass_for_piano(
            inst,
            bool(p["bypass"]),
            session.restored_piano,
            session.piano_ids,
        )
        if _apply_plugin_bypass(inst, bypass_on, session.piano_ids):
            session.active_piano = inst


def print_restored_state(
    restored_piano: Optional[int],
    restored_cc: list[tuple[int, str, float]],
) -> None:
    if restored_piano is not None:
        print(f"[State] Restored last active piano: {restored_piano}")
    for inst, symbol, fval in restored_cc:
        print(f"[State] Restored CC param {inst}:{symbol}={fval}")


def print_startup_piano_status(active_piano: Optional[int]) -> None:
    if active_piano is not None:
        print(f"[Startup] Active piano: {active_piano}")
        return
    print(
        "[Startup] Warning: No active piano (all pianos bypassed)."
        " Send a Program Change to select one."
    )


def apply_startup_output_gain(session: RouterSession) -> None:
    if session.output_gain_instance is None or session.active_piano is None:
        return
    startup_gain = session.program_gains.get(
        session.active_piano, session.output_gain_default
    )
    print(f"== output gain for {session.active_piano}: {startup_gain} dB")
    try:
        mod_param_set(
            session.output_gain_instance, OUTPUT_GAIN_PARAM, startup_gain
        )
    except Exception as e:
        print(f"Failed output gain set for {session.active_piano}: {e}")


def connect_pedalboard_ports(
    connections: list[dict[str, str]],
) -> list[tuple[str, str]]:
    active_connections: list[tuple[str, str]] = []
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
    return active_connections


def maybe_send_fluida_preset(
    session: RouterSession,
    instance: int,
    preset: Optional[int] = None,
) -> None:
    if session.jack_client is None or session.preset_out_port is None:
        return
    base = session.fluida_presets.get(instance)
    if base is None:
        for src, dst in reversed(session.jack_midi_connections):
            if src is not session.preset_out_port:
                continue
            try:
                session.jack_client.disconnect(src, dst)
                session.jack_midi_connections.remove((src, dst))
            except jack.JackError:
                pass
        return
    config = FluidaPresetConfig(
        preset=base.preset if preset is None else preset,
        bank_msb=base.bank_msb,
        bank_lsb=base.bank_lsb,
    )
    send_fluida_preset(
        session.jack_client,
        session.preset_out_port,
        instance,
        config,
        session.jack_midi_connections,
    )


def sync_sl88_to_active_piano(session: RouterSession) -> None:
    if session.active_piano is None:
        return
    if (
        session.jack_client is None
        or session.out_port is None
        or session.in_port is None
    ):
        return

    print(
        f"[SL88 Sync] Attempting to sync SL88 to active piano"
        f" {session.active_piano}..."
    )
    src_name = session.out_port.name
    dst_name = SL88_TARGET_PORT
    sl_dest = session.jack_client.get_port_by_name(SL88_TARGET_PORT)

    if dst_name == session.in_port.name:
        print(
            f"[SL88 Sync] ERROR: Refusing to connect {src_name} -> {dst_name}"
            " (self-loop)"
        )
        maybe_send_fluida_preset(session, session.active_piano)
        return

    try:
        session.jack_client.connect(session.out_port, sl_dest)
        session.jack_midi_connections.append((session.out_port, sl_dest))
        print(f"[SL88 Sync] Connected {src_name} -> {dst_name}")

        status = 0xC0 | (COMMON_CHANNEL - 1)
        msg_bytes = bytes([status, session.active_piano])
        send_q.put(msg_bytes)
        print(
            f"[SL88 Sync] Queued ONE-SHOT Program Change: {session.active_piano}"
            f" on Ch{COMMON_CHANNEL} (Hex: {msg_bytes.hex()})"
        )
    except Exception as e:
        print(f"[SL88 Sync] Failed: {e}")

    maybe_send_fluida_preset(session, session.active_piano)


def setup_midi_input_listeners(session: RouterSession) -> None:
    if session.jack_client is None or session.in_port is None:
        return

    print("Starting JACK MIDI listener for Program Changes...")
    print(f"Listening on: {session.jack_client.name}:input")
    connect_jack_midi_source(
        session.jack_client, TARGET_PORT, session.in_port, session.jack_midi_connections
    )

    print(f"Listening for Control Changes on ch{CC_CHANNEL}...")
    print(f"Listening on: {session.jack_client.name}:cc_input")
    if session.cc_map:
        for cc_num in sorted(session.cc_map):
            m = session.cc_map[cc_num]
            kind = "pass-through" if m.pass_through else "param"
            print(f"  CC {cc_num} -> {m.instance}:{m.param} ({kind})")
        takeover = "on" if CC_SOFT_TAKEOVER else "off"
        print(f"  Soft takeover: {takeover} (threshold={CC_PICKUP_THRESHOLD})")
        connect_jack_midi_source(
            session.jack_client,
            CC_TARGET_PORT,
            session.cc_in_port,
            session.jack_midi_connections,
        )
    else:
        print("  (no plugin midi_cc mappings in pedalboard)")

    if session.lcxl_out_port is not None:
        connect_lcxl_daw_in(
            session.jack_client,
            session.lcxl_out_port,
            session.jack_midi_connections,
        )

    if session.active_piano is not None:
        configure_lcxl_for_plugin(session, session.active_piano)

    if session.fluida_presets:
        print(
            f"Listening for Fluida preset CC {FLUIDA_PRESET_CC}"
            f" on ch{COMMON_CHANNEL} (input + cc_input ports)"
        )

    print("Listening for MIDI events... (Ctrl+C to stop)")
    print(
        "Mapping: Program Change X -> Piano Instance X."
        f" Detected Pianos: {sorted(session.piano_ids)}"
    )


def arm_cc_soft_takeover(
    active_piano: Optional[int],
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
    cc_pickup: dict[int, CcPickupState],
    cc_mapped_instances: set[int],
    *,
    prefix: str = "[CC]",
) -> None:
    if not CC_SOFT_TAKEOVER or active_piano not in cc_mapped_instances:
        return
    reset_ccs = reset_pickup_for_instance(
        active_piano, cc_map, applied_params, cc_pickup
    )
    if reset_ccs:
        print(
            f"{prefix} Soft takeover armed for instance {active_piano}"
            f" (CCs {reset_ccs})"
        )


def _drain_aux_midi_queues(
    session: RouterSession,
    cc_pickup: dict[int, CcPickupState],
    listener_state: MidiListenerState,
    persist_state_fn: Callable[..., None],
) -> None:
    drain_cc_events(
        listener_state.last_seen_midi,
        session.cc_map,
        session.applied_params,
        cc_pickup,
        session.active_piano,
        persist_state_fn,
    )
    drain_fluida_preset_cc_events(
        cc_event_q,
        source="cc_input",
        active_piano=session.active_piano,
        fluida_presets=session.fluida_presets,
        fluida_instance_ids=session.fluida_instance_ids,
        last_fluida_preset_cc=listener_state.last_fluida_preset_cc,
        send_preset=lambda inst, preset: maybe_send_fluida_preset(
            session, inst, preset
        ),
    )


def _warn_undecodable_program_change(data: bytes) -> None:
    if data and len(data) >= 2 and (data[0] & 0xF0) == 0xC0:
        print(f"[MIDI] Warning: undecodable program change: {data.hex()}")


def _should_ignore_program_change(msg, last_prog: Optional[int]) -> bool:
    if msg.type != "program_change":
        return True
    if FILTER_CHANNEL is not None and msg.channel != FILTER_CHANNEL:
        return True
    return msg.program == last_prog


def _set_piano_bypass_states(piano_ids: list[int], active_inst: int) -> None:
    for inst in piano_ids:
        bypass_val = inst != active_inst
        try:
            mod_bypass(inst, bypass_val)
        except Exception as e:
            print(f"   Failed to set bypass for {inst}: {e}")


def _apply_output_gain_for_piano(session: RouterSession, prog: int) -> None:
    if session.output_gain_instance is None:
        return
    switch_gain = session.program_gains.get(prog, session.output_gain_default)
    print(f"   Setting output gain to {switch_gain} dB")
    try:
        mod_param_set(session.output_gain_instance, OUTPUT_GAIN_PARAM, switch_gain)
    except Exception as e:
        print(f"   Failed output gain set for {prog}: {e}")


def switch_active_piano(
    session: RouterSession,
    prog: int,
    cc_pickup: dict[int, CcPickupState],
    persist_state_fn: Callable[..., None],
) -> None:
    print(f"   Selecting Piano {prog}...")
    _set_piano_bypass_states(session.piano_ids, prog)
    arm_cc_soft_takeover(
        prog,
        session.cc_map,
        session.applied_params,
        cc_pickup,
        session.cc_mapped_instances,
        prefix="   [CC]",
    )
    _apply_output_gain_for_piano(session, prog)
    session.active_piano = prog
    maybe_send_fluida_preset(session, prog)
    configure_lcxl_for_plugin(session, prog)
    try:
        persist_state_fn(force=True)
        print(f"   [State] Saved active piano {prog} to {STATE_FILE}")
    except Exception as e:
        print(f"   [State] Failed to save state: {e}")


def _handle_program_change_message(
    msg,
    session: RouterSession,
    cc_pickup: dict[int, CcPickupState],
    listener_state: MidiListenerState,
    persist_state_fn: Callable[..., None],
) -> bool:
    """Process a program change. Returns True when the listener loop should stop."""
    if _should_ignore_program_change(msg, listener_state.last_prog):
        return False

    prog = msg.program
    listener_state.last_prog = prog

    if prog == KILL_PC:
        print("[midi-shutdown] Shutdown via Program Change")
        subprocess.run(["sudo", "/bin/systemctl", "poweroff"])
        return True

    print(f"🎹 PROGRAM CHANGE -> program={prog}, channel={msg.channel}")
    if prog in session.piano_ids:
        switch_active_piano(session, prog, cc_pickup, persist_state_fn)
    else:
        print(
            f"   (Program {prog} is not a known piano instance, ignoring switch)"
        )
    return False


def _process_incoming_midi_event(
    data: bytes,
    session: RouterSession,
    cc_pickup: dict[int, CcPickupState],
    listener_state: MidiListenerState,
    persist_state_fn: Callable[..., None],
) -> bool:
    """Handle one queued MIDI event. Returns True when the listener loop should stop."""
    _drain_aux_midi_queues(session, cc_pickup, listener_state, persist_state_fn)

    msg = decode_mido(data)
    if msg is None:
        _warn_undecodable_program_change(data)
        return False

    if handle_fluida_preset_cc(
        msg,
        active_piano=session.active_piano,
        fluida_presets=session.fluida_presets,
        fluida_instance_ids=session.fluida_instance_ids,
        last_fluida_preset_cc=listener_state.last_fluida_preset_cc,
        send_preset=lambda inst, preset: maybe_send_fluida_preset(
            session, inst, preset
        ),
        source="input",
    ):
        return False

    return _handle_program_change_message(
        msg, session, cc_pickup, listener_state, persist_state_fn
    )


def run_midi_event_loop(
    session: RouterSession,
    cc_pickup: dict[int, CcPickupState],
    persist_state_fn: Callable[..., None],
) -> None:
    listener_state = MidiListenerState()
    arm_cc_soft_takeover(
        session.active_piano,
        session.cc_map,
        session.applied_params,
        cc_pickup,
        session.cc_mapped_instances,
    )

    while not stop_event.is_set():
        try:
            data = event_q.get(timeout=0.25)
        except queue.Empty:
            _drain_aux_midi_queues(
                session, cc_pickup, listener_state, persist_state_fn
            )
            continue

        if _process_incoming_midi_event(
            data, session, cc_pickup, listener_state, persist_state_fn
        ):
            break


def run_loader_session(
    session: RouterSession,
    restored_cc: list[tuple[int, str, float]],
    persist_state_fn: Callable[..., None],
) -> None:
    sweep_stale_plugins(session.plugin_ids)

    (
        session.jack_client,
        session.in_port,
        session.cc_in_port,
        session.out_port,
        session.preset_out_port,
        session.lcxl_out_port,
        session.cc_forward_out_port,
    ) = create_jack_client()
    print(f"Opened JACK client: {session.jack_client.name}")

    session.output_gain_instance, session.output_gain_default = find_output_gain_config(
        session.plugins
    )
    load_pedalboard_plugins(session)

    session.restored_piano = normalize_restored_piano(
        session.restored_piano, session.piano_ids
    )
    print_restored_state(session.restored_piano, restored_cc)
    apply_pedalboard_state(session)

    cc_pickup = init_cc_pickup(session.cc_map, session.applied_params)
    print_startup_piano_status(session.active_piano)
    apply_startup_output_gain(session)

    time.sleep(0.2)
    session.active_connections = connect_pedalboard_ports(session.connections)

    print("== done loading ==")
    print("---------------------------------------------------")

    session.jack_client.activate()
    session.jack_activated = True
    print(f"Activated JACK client: {session.jack_client.name}")

    sync_sl88_to_active_piano(session)
    setup_midi_input_listeners(session)
    run_midi_event_loop(session, cc_pickup, persist_state_fn)

    if _shutdown_signal is not None:
        log(f"Received signal {_shutdown_signal}, stopping...")


def main() -> None:
    pb = load_pedalboard_from_argv()
    session, restored_cc = init_router_session(pb)

    print("== Loading Plugins == ")

    def persist_state(force: bool = False) -> None:
        save_router_state(
            session.active_piano, session.applied_params, session.cc_map, force=force
        )

    try:
        run_loader_session(session, restored_cc, persist_state)
    except KeyboardInterrupt:
        log("Stopping...")
    except Exception as e:
        log(f"Session error: {e}")
        raise
    finally:
        cleanup_session(
            loaded_ids=session.loaded_ids,
            active_connections=session.active_connections,
            jack_midi_connections=session.jack_midi_connections,
            jack_client=session.jack_client,
            jack_activated=session.jack_activated,
            persist_state_fn=persist_state,
        )


if __name__ == "__main__":
    main()
  
