"""Small client helpers for the mod-host text protocol."""

from __future__ import annotations

import os
import socket
from typing import Any, Optional

MOD_HOST = os.environ.get("MOD_HOST", "127.0.0.1")
MOD_PORT = int(os.environ.get("MOD_PORT", "5555"))
TIMEOUT_S = float(os.environ.get("MOD_TIMEOUT", "5.0"))


def send_cmd(line: str) -> str:
    """Send one mod-host command and return response text."""
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

    resp = resp.replace(b"\x00", b"")
    return resp.decode("utf-8", errors="replace").strip()


def parse_resp(resp: str) -> Optional[int]:
    """Parse ``resp <int>`` and return the integer code."""
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
    """Accept any non-negative response code as success; return the code."""
    code = parse_resp(resp)
    if code is None:
        raise RuntimeError(f"{what} failed (unparseable): {resp}")
    if code < 0:
        raise RuntimeError(f"{what} failed: {resp}")
    return code


def expect_zero(resp: str, what: str) -> None:
    """Require ``resp 0``."""
    code = parse_resp(resp)
    if code != 0:
        raise RuntimeError(f"{what} failed: {resp}")


def mod_preload(uri: str, instance_id: int) -> None:
    resp = send_cmd(f'preload "{uri}" {instance_id}')
    code = expect_nonnegative(resp, f"add {instance_id} {uri}")
    if code != instance_id:
        print(f"WARNING: add requested id={instance_id} but host returned resp {code}")


def mod_bypass(inst: int, bypass_on: bool) -> None:
    resp = send_cmd(f"bypass {inst} {1 if bypass_on else 0}")
    expect_zero(resp, f"bypass {inst}")


def mod_add(uri: str, instance_id: int) -> None:
    resp = send_cmd(f'add "{uri}" {instance_id}')
    code = expect_nonnegative(resp, f"add {instance_id} {uri}")
    if code != instance_id:
        print(f"WARNING: add requested id={instance_id} but host returned resp {code}")


def mod_param_set(instance_id: int, symbol: str, value: Any) -> None:
    resp = send_cmd(f"param_set {instance_id} {symbol} {value}")
    expect_zero(resp, f"param_set {instance_id} {symbol}")


def mod_patch_set(instance_id: int, key: str, value: str) -> None:
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
