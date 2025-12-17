#!/usr/bin/env python3
import socket
import sys

# ---- config ----
MOD_HOST = "127.0.0.1"
MOD_PORT = 5555
TIMEOUT_S = 2.0
# ----------------

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

    # mod-host may include NUL bytes
    resp = resp.replace(b"\x00", b"")
    return resp.decode("utf-8", errors="replace").strip()


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  modhost_cmd.py <command>")
        print("Examples:")
        print("  modhost_cmd.py \"list\"")
        print("  modhost_cmd.py \"bypass 42 1\"")
        print("  modhost_cmd.py \"param_set 7 Gain -3.0\"")
        sys.exit(1)

    cmd = " ".join(sys.argv[1:])
    resp = send_cmd(cmd)

    if resp:
        print(resp)


if __name__ == "__main__":
    main()
