#!/usr/bin/env python3
"""
Headless MOD-style pedalboard.json loader for mod-host.

- Reads a pedalboard.json (MOD v2-ish) containing:
  - plugins: { "<id>": { "uri": "...", "controls": {...}, "state": {...} } }
  - connections: [ { "from": "...", "to": "..." }, ... ]

- Talks to mod-host over TCP (default 127.0.0.1:5555) using its text protocol.

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
from pathlib import Path
from typing import Any, Optional


MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))


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


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/pedalboard.json", file=sys.stderr)
        sys.exit(2)

    pb_path = Path(sys.argv[1])
    pb = json.loads(pb_path.read_text(encoding="utf-8"))

    plugins: dict[str, Any] = pb.get("plugins", {})
    connections: list[dict[str, str]] = pb.get("connections", [])

    # 1) Add plugins (sorted by numeric id for deterministic behavior)
    for sid in sorted(plugins.keys(), key=lambda x: int(x)):
        p = plugins[sid]
        uri = p["uri"]
        inst = int(sid)
        print(f'== add {inst} {uri}')
        mod_add(uri, inst)

    # 2) Apply state (patch_set) and controls (param_set)
    for sid in sorted(plugins.keys(), key=lambda x: int(x)):
        p = plugins[sid]
        inst = int(sid)

        state = p.get("state", {}) or {}
        for key, val in state.items():
            print(f"== patch_set {inst} {key} = {val}")
            mod_patch_set(inst, key, str(val))

        controls = p.get("controls", {}) or {}
        for symbol, val in controls.items():
            print(f"== param_set {inst} {symbol} {val}")
            mod_param_set(inst, symbol, val)

        # 3) Optional bypass flag (boolean)
        if "bypass" in p:
            bypass_on = bool(p["bypass"])
            print(f"== bypass {inst} {1 if bypass_on else 0}")
            mod_bypass(inst, bypass_on)

    # Small delay helps samplers settle before wiring audio
    time.sleep(0.2)

    # 3) Connect ports
    for c in connections:
        src = expand_port(c["from"])
        dst = expand_port(c["to"])
        print(f"== connect {src} -> {dst}")
        mod_connect(src, dst)

    print("== done ==")


if __name__ == "__main__":
    main()
