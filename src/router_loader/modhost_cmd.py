#!/usr/bin/env python3
import sys

from .modhost import send_cmd


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  router-modhost-cmd <command>")
        print("Examples:")
        print("  router-modhost-cmd \"list\"")
        print("  router-modhost-cmd \"bypass 42 1\"")
        print("  router-modhost-cmd \"param_set 7 Gain -3.0\"")
        sys.exit(1)

    cmd = " ".join(sys.argv[1:])
    resp = send_cmd(cmd)

    if resp:
        print(resp)


if __name__ == "__main__":
    main()
