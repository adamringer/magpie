#!/usr/bin/env python3
"""Run shell commands on the Clip over telnet (root shell, no login).

    .venv/bin/python -I -W ignore tools/magpie_sh.py 'uname -a' 'cat /proc/mtd'
"""
import sys
import telnetlib

PROMPT = b"# "  # stock shell shows "/ # ", our BusyBox "# "
MARKER = b"__MAGPIE_PROMPT__# "


def main():
    t = telnetlib.Telnet("192.168.2.2", 23, timeout=10)
    t.read_until(PROMPT, timeout=10)
    # A unique prompt, so command output that contains "/ # " can't end a read early.
    t.write(b"PS1='" + MARKER + b"'\n")
    t.read_until(MARKER, timeout=10)
    t.read_until(MARKER, timeout=10)
    for cmd in sys.argv[1:]:
        t.write(cmd.encode() + b"\n")
        out = t.read_until(MARKER, timeout=30).decode(errors="replace")
        # Drop the echoed command line and the trailing prompt.
        body = out.split("\n", 1)[1] if "\n" in out else ""
        print(f"### {cmd}\n{body.removesuffix(MARKER.decode()).rstrip()}\n")
    t.write(b"exit\n")


if __name__ == "__main__":
    main()
