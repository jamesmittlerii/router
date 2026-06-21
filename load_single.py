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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import jack
import mido

# ---- Configuration ----

MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))
COMMON_CHANNEL = 2  # User confirmed Channel 2
FLUIDA_URI = "https://github.com/brummer10/Fluida.lv2"
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
CC_TARGET_PORT = os.environ.get("CC_TARGET_PORT", "system:midi_capture_5")
CC_CHANNEL = int(os.environ.get("CC_CHANNEL", "3"))  # 1-16
CC_SOFT_TAKEOVER = os.environ.get("CC_SOFT_TAKEOVER", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CC_PICKUP_THRESHOLD = max(0, min(127, int(os.environ.get("CC_PICKUP_THRESHOLD", "1"))))
CC_STATE_SAVE_DEBOUNCE_S = float(os.environ.get("CC_STATE_SAVE_DEBOUNCE_S", "0.5"))

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


def parse_midi_cc_entry(raw: Any) -> tuple[str, float, float]:
    """Parse a midi_cc map value (symbol string or {param, min, max} object)."""
    if isinstance(raw, str):
        return raw, 0.0, 1.0
    if isinstance(raw, dict):
        param = raw.get("param") or raw.get("symbol")
        if not param:
            raise ValueError("midi_cc entry dict needs 'param' or 'symbol'")
        try:
            min_val = float(raw.get("min", 0.0))
            max_val = float(raw.get("max", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid min/max in midi_cc entry: {raw}") from exc
        return str(param), min_val, max_val
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
                param, min_val, max_val = parse_midi_cc_entry(entry)
            except (TypeError, ValueError) as exc:
                print(f"Warning: skipping midi_cc {sid}[{cc_key!r}]: {exc}")
                continue
            if cc_num in cc_map:
                prev = cc_map[cc_num]
                print(
                    f"Warning: CC {cc_num} remapped {prev.instance}:{prev.param}"
                    f" -> {inst}:{param}"
                )
            cc_map[cc_num] = CcParamMapping(inst, param, min_val, max_val)
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
        controls = plugin.get("controls") or {}
        if not isinstance(controls, dict):
            continue
        inst = int(sid)
        for symbol, val in controls.items():
            try:
                applied[(inst, str(symbol))] = float(val)
            except (TypeError, ValueError):
                continue
    return applied


def init_cc_pickup(
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
) -> dict[int, CcPickupState]:
    pickup: dict[int, CcPickupState] = {}
    for cc, mapping in cc_map.items():
        param_val = applied_params.get(
            (mapping.instance, mapping.param),
            mapping.min_val,
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
            mapping.min_val,
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


def create_jack_client() -> tuple[jack.Client, jack.Port, jack.Port, jack.Port, jack.Port]:
    """Open the router JACK MIDI client (retries if a stale client name lingers)."""
    last_err: Optional[Exception] = None
    for attempt in range(1, JACK_OPEN_RETRIES + 1):
        try:
            c = jack.Client(JACK_CLIENT_NAME, no_start_server=True)
            inp = c.midi_inports.register("input")
            cc_inp = c.midi_inports.register("cc_input")
            outp = c.midi_outports.register("output")
            preset_outp = c.midi_outports.register("preset_out")

            @c.set_process_callback
            def process(frames, _inp=inp, _cc_inp=cc_inp, _outp=outp, _preset_outp=preset_outp):
                for offset, data in _inp.incoming_midi_events():
                    try:
                        event_q.put_nowait(bytes(data))
                    except queue.Full:
                        pass

                for offset, data in _cc_inp.incoming_midi_events():
                    try:
                        cc_event_q.put_nowait(bytes(data))
                    except queue.Full:
                        pass

                _outp.clear_buffer()
                while True:
                    try:
                        msg = send_q.get_nowait()
                        _outp.write_midi_event(0, msg)
                    except queue.Empty:
                        break

                _preset_outp.clear_buffer()
                while True:
                    try:
                        msg = fluida_send_q.get_nowait()
                        _preset_outp.write_midi_event(0, msg)
                    except queue.Empty:
                        break

            return c, inp, cc_inp, outp, preset_outp
        except jack.JackOpenError as exc:
            last_err = exc
            if attempt < JACK_OPEN_RETRIES:
                print(
                    f"JACK client '{JACK_CLIENT_NAME}' unavailable"
                    f" (attempt {attempt}/{JACK_OPEN_RETRIES}): {exc}"
                )
                time.sleep(JACK_OPEN_RETRY_DELAY_S)
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


def drain_cc_events(
    last_seen_midi: dict[int, int],
    cc_map: dict[int, CcParamMapping],
    applied_params: dict[tuple[int, str], float],
    cc_pickup: dict[int, CcPickupState],
    persist_state: Optional[Callable[..., None]] = None,
) -> None:
    """Apply control changes on CC_CHANNEL via mod-host param_set."""
    if not cc_map:
        return

    cc_mido_channel = CC_CHANNEL - 1
    while True:
        try:
            data = cc_event_q.get_nowait()
        except queue.Empty:
            break

        msg = decode_mido(data)
        if msg is None or msg.type != "control_change":
            continue
        if msg.channel != cc_mido_channel:
            continue

        mapping = cc_map.get(msg.control)
        if mapping is None:
            continue
        if last_seen_midi.get(msg.control) == msg.value:
            continue

        last_seen_midi[msg.control] = msg.value
        state = cc_pickup.get(msg.control)
        if state is None:
            state = CcPickupState()
            cc_pickup[msg.control] = state

        if CC_SOFT_TAKEOVER:
            if not pickup_should_apply(state, msg.value):
                state.last_midi = msg.value
                continue
            if not state.armed:
                state.armed = True
                print(
                    f"🎚️  CC ch{CC_CHANNEL} cc={msg.control} pickup"
                    f" (target={state.target_midi}, fader={msg.value})"
                )

        state.last_midi = msg.value
        param_val = midi_cc_to_param(msg.value, mapping)
        try:
            mod_param_set(mapping.instance, mapping.param, param_val)
        except Exception as e:
            print(
                f"Failed CC ch{CC_CHANNEL} cc={msg.control}"
                f" -> {mapping.instance}:{mapping.param}: {e}"
            )
            continue

        applied_params[(mapping.instance, mapping.param)] = param_val
        state.target_midi = msg.value

        print(
            f"🎚️  CC ch{CC_CHANNEL} cc={msg.control}"
            f" -> {mapping.instance}:{mapping.param}={param_val:.3f}"
        )
        if persist_state is not None:
            persist_state()


def handle_fluida_preset_cc(
    msg,
    *,
    active_piano: Optional[int],
    fluida_presets: dict[int, FluidaPresetConfig],
    last_fluida_preset_cc: dict[int, int],
    send_preset: Callable[[int, int], None],
) -> bool:
    """Apply a COMMON_CHANNEL CC as a Fluida preset change. Returns True if handled."""
    if msg.type != "control_change":
        return False
    if msg.channel != COMMON_CHANNEL - 1:
        return False
    if msg.control != FLUIDA_PRESET_CC:
        return False
    if active_piano is None or active_piano not in fluida_presets:
        return False

    preset = max(0, min(127, msg.value))
    if last_fluida_preset_cc.get(active_piano) == preset:
        return True

    last_fluida_preset_cc[active_piano] = preset
    send_preset(active_piano, preset)
    print(
        f"🎹 FLUIDA PRESET CC ch{COMMON_CHANNEL} cc={FLUIDA_PRESET_CC}"
        f" -> instance {active_piano} preset={preset}"
    )
    return True

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
    cc_map = build_cc_map(plugins)
    applied_params = seed_applied_params(plugins)
    restored_piano, saved_plugin_controls = load_router_state()
    restored_cc = merge_saved_plugin_controls(
        applied_params, saved_plugin_controls, cc_map
    )
    cc_mapped_instances = instances_with_cc_maps(cc_map)
    plugin_ids = sorted((int(sid) for sid in plugins.keys()))

    print("== Loading Plugins == ")
    piano_ids = []
    active_piano = None
    program_gains: dict[int, float] = {}
    fluida_presets: dict[int, FluidaPresetConfig] = {}
    output_gain_instance: Optional[int] = None
    output_gain_default = 0.0

    loaded_ids: list[int] = []
    active_connections: list[tuple[str, str]] = []
    jack_midi_connections: list[tuple[jack.Port, jack.Port]] = []
    jack_client: Optional[jack.Client] = None
    jack_activated = False
    in_port: Optional[jack.Port] = None
    cc_in_port: Optional[jack.Port] = None
    out_port: Optional[jack.Port] = None
    preset_out_port: Optional[jack.Port] = None

    def maybe_send_fluida_preset(instance: int, preset: Optional[int] = None) -> None:
        if jack_client is None or preset_out_port is None:
            return
        base = fluida_presets.get(instance)
        if base is None:
            for src, dst in reversed(jack_midi_connections):
                if src is preset_out_port:
                    try:
                        jack_client.disconnect(src, dst)
                        jack_midi_connections.remove((src, dst))
                    except jack.JackError:
                        pass
            return
        config = FluidaPresetConfig(
            preset=base.preset if preset is None else preset,
            bank_msb=base.bank_msb,
            bank_lsb=base.bank_lsb,
        )
        send_fluida_preset(
            jack_client,
            preset_out_port,
            instance,
            config,
            jack_midi_connections,
        )

    def persist_state(force: bool = False) -> None:
        save_router_state(active_piano, applied_params, cc_map, force=force)

    try:
        sweep_stale_plugins(plugin_ids)

        jack_client, in_port, cc_in_port, out_port, preset_out_port = create_jack_client()
        print(f"Opened JACK client: {jack_client.name}")

        for sid, plugin in plugins.items():
            if not isinstance(plugin, dict):
                continue
            if plugin.get("uri") != OUTPUT_GAIN_URI:
                continue

            output_gain_instance = int(sid)
            out_controls = plugin.get("controls", {})
            if isinstance(out_controls, dict):
                try:
                    output_gain_default = float(out_controls.get(OUTPUT_GAIN_PARAM, 0.0))
                except (TypeError, ValueError):
                    output_gain_default = 0.0
            break

        for sid in sorted(plugins.keys(), key=lambda x: int(x)):
            p = plugins[sid]
            uri = p["uri"]
            inst = int(sid)
            if uri in (
               "http://sfztools.github.io/sfizz",
               FLUIDA_URI,
               "http://studionumbersix.com/foo/lv2/yc20",
               "http://bristol.sourceforge.net/lv2/vox",
               "https://ho-ro.net/connie/lv2",
            ):
                piano_ids.append(inst)
                program_gains[inst] = get_plugin_gain(p, output_gain_default)

            if uri == FLUIDA_URI:
                preset_cfg = parse_fluida_preset(p)
                if preset_cfg is not None:
                    fluida_presets[inst] = preset_cfg

            print(f'== add {inst} {uri}')
            try:
                mod_add(uri, inst)
                loaded_ids.append(inst)
            except Exception as e:
                print(f"Failed to add plugin {inst}: {e}")

        restored_piano = normalize_restored_piano(restored_piano, piano_ids)

        if restored_piano is not None:
            print(f"[State] Restored last active piano: {restored_piano}")
        for inst, symbol, fval in restored_cc:
            print(f"[State] Restored CC param {inst}:{symbol}={fval}")

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

            if "bypass" in p:
                bypass_on = bool(p["bypass"])

                if restored_piano is not None and inst in piano_ids:
                    if inst == restored_piano:
                        bypass_on = False
                    else:
                        bypass_on = True

                print(f"== bypass {inst} {1 if bypass_on else 0}")
                try:
                    mod_bypass(inst, bypass_on)
                    if not bypass_on and inst in piano_ids:
                        active_piano = inst
                except Exception as e:
                    print(f"Failed bypass {inst}: {e}")

        cc_pickup = init_cc_pickup(cc_map, applied_params)

        if active_piano is not None:
            print(f"[Startup] Active piano: {active_piano}")
        else:
            print(
                "[Startup] Warning: No active piano (all pianos bypassed)."
                " Send a Program Change to select one."
            )

        if output_gain_instance is not None and active_piano is not None:
            startup_gain = program_gains.get(active_piano, output_gain_default)
            print(f"== output gain for {active_piano}: {startup_gain} dB")
            try:
                mod_param_set(output_gain_instance, OUTPUT_GAIN_PARAM, startup_gain)
            except Exception as e:
                print(f"Failed output gain set for {active_piano}: {e}")

        time.sleep(0.2)

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

        jack_client.activate()
        jack_activated = True
        print(f"Activated JACK client: {jack_client.name}")

        if active_piano is not None:
            print(f"[SL88 Sync] Attempting to sync SL88 to active piano {active_piano}...")

            target_port_name = "system:midi_playback_1"
            src_name = out_port.name
            dst_name = target_port_name
            sl_dest = jack_client.get_port_by_name(target_port_name)

            if dst_name == in_port.name:
                print(f"[SL88 Sync] ERROR: Refusing to connect {src_name} -> {dst_name} (self-loop)")
            else:
                try:
                    jack_client.connect(out_port, sl_dest)
                    jack_midi_connections.append((out_port, sl_dest))
                    print(f"[SL88 Sync] Connected {src_name} -> {dst_name}")

                    status = 0xC0 | (COMMON_CHANNEL - 1)
                    msg_bytes = bytes([status, active_piano])
                    send_q.put(msg_bytes)
                    print(
                        f"[SL88 Sync] Queued ONE-SHOT Program Change: {active_piano}"
                        f" on Ch{COMMON_CHANNEL} (Hex: {msg_bytes.hex()})"
                    )
                except Exception as e:
                    print(f"[SL88 Sync] Failed: {e}")

            maybe_send_fluida_preset(active_piano)

        print("Starting JACK MIDI listener for Program Changes...")
        print(f"Listening on: {jack_client.name}:input")
        connect_jack_midi_source(jack_client, TARGET_PORT, in_port, jack_midi_connections)

        print(f"Listening for Control Changes on ch{CC_CHANNEL}...")
        print(f"Listening on: {jack_client.name}:cc_input")
        if cc_map:
            for cc_num in sorted(cc_map):
                m = cc_map[cc_num]
                print(f"  CC {cc_num} -> {m.instance}:{m.param}")
            takeover = "on" if CC_SOFT_TAKEOVER else "off"
            print(f"  Soft takeover: {takeover} (threshold={CC_PICKUP_THRESHOLD})")
            connect_jack_midi_source(
                jack_client, CC_TARGET_PORT, cc_in_port, jack_midi_connections
            )
        else:
            print("  (no plugin midi_cc mappings in pedalboard)")

        if fluida_presets:
            print(
                f"Listening for Fluida preset CC {FLUIDA_PRESET_CC}"
                f" on ch{COMMON_CHANNEL} (same port as Program Changes)"
            )

        print("Listening for MIDI events... (Ctrl+C to stop)")
        print(f"Mapping: Program Change X -> Piano Instance X. Detected Pianos: {sorted(piano_ids)}")

        last_prog = None
        last_seen_midi: dict[int, int] = {}
        last_fluida_preset_cc: dict[int, int] = {}

        if CC_SOFT_TAKEOVER and active_piano in cc_mapped_instances:
            reset_ccs = reset_pickup_for_instance(
                active_piano, cc_map, applied_params, cc_pickup
            )
            if reset_ccs:
                print(
                    f"[CC] Soft takeover armed for instance {active_piano}"
                    f" (CCs {reset_ccs})"
                )

        while not stop_event.is_set():
            try:
                data = event_q.get(timeout=0.25)
            except queue.Empty:
                drain_cc_events(
                    last_seen_midi, cc_map, applied_params, cc_pickup, persist_state
                )
                continue

            drain_cc_events(
                last_seen_midi, cc_map, applied_params, cc_pickup, persist_state
            )

            msg = decode_mido(data)
            if msg is None:
                if data and len(data) >= 2 and (data[0] & 0xF0) == 0xC0:
                    print(f"[MIDI] Warning: undecodable program change: {data.hex()}")
                continue

            if handle_fluida_preset_cc(
                msg,
                active_piano=active_piano,
                fluida_presets=fluida_presets,
                last_fluida_preset_cc=last_fluida_preset_cc,
                send_preset=lambda inst, preset: maybe_send_fluida_preset(inst, preset),
            ):
                continue

            if msg.type != "program_change":
                continue

            if FILTER_CHANNEL is not None and msg.channel != FILTER_CHANNEL:
                continue

            prog = msg.program

            # Optional debounce
            if prog == last_prog:
                continue
            last_prog = prog

            if prog == KILL_PC:
                print("[midi-shutdown] Shutdown via Program Change")
                subprocess.run(["sudo", "/bin/systemctl", "poweroff"])
                break

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

                if CC_SOFT_TAKEOVER and prog in cc_mapped_instances:
                    reset_ccs = reset_pickup_for_instance(
                        prog, cc_map, applied_params, cc_pickup
                    )
                    if reset_ccs:
                        print(
                            f"   [CC] Soft takeover armed for instance {prog}"
                            f" (CCs {reset_ccs})"
                        )

                if output_gain_instance is not None:
                    switch_gain = program_gains.get(prog, output_gain_default)
                    print(f"   Setting output gain to {switch_gain} dB")
                    try:
                        mod_param_set(output_gain_instance, OUTPUT_GAIN_PARAM, switch_gain)
                    except Exception as e:
                        print(f"   Failed output gain set for {prog}: {e}")

                active_piano = prog
                maybe_send_fluida_preset(prog)
                try:
                    persist_state(force=True)
                    print(f"   [State] Saved active piano {prog} to {STATE_FILE}")
                except Exception as e:
                     print(f"   [State] Failed to save state: {e}")
            else:
                print(f"   (Program {prog} is not a known piano instance, ignoring switch)")

        if _shutdown_signal is not None:
            log(f"Received signal {_shutdown_signal}, stopping...")

    except KeyboardInterrupt:
        log("Stopping...")
    except Exception as e:
        log(f"Session error: {e}")
        raise
    finally:
        cleanup_session(
            loaded_ids=loaded_ids,
            active_connections=active_connections,
            jack_midi_connections=jack_midi_connections,
            jack_client=jack_client,
            jack_activated=jack_activated,
            persist_state_fn=persist_state,
        )


if __name__ == "__main__":
    main()
  
