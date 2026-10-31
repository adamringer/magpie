#!/usr/bin/env python3
"""Dump a block device from the Clip in chunks, via /tmp and ftpd, checking each chunk's MD5.

ftpd only serves regular files, and /tmp (tmpfs) has room for about 6 MB, so
each chunk is dd'd to /tmp/chunk.bin, fetched, checked and removed.

    .venv/bin/python -I -W ignore tools/magpie_dump.py /dev/mmcblk0 backup/mmcblk0_first32M.bin --mib 32
"""
import argparse
import ftplib
import hashlib
import telnetlib

HOST = "192.168.2.2"
MARKER = b"__MAGPIE_PROMPT__# "
CHUNK_MIB = 4
REMOTE = "/tmp/chunk.bin"


class Shell:
    def __init__(self):
        self.t = telnetlib.Telnet(HOST, 23, timeout=10)
        self.t.read_until(b"# ", timeout=10)
        self.t.write(b"PS1='" + MARKER + b"'\n")
        self.t.read_until(MARKER, timeout=10)
        self.t.read_until(MARKER, timeout=10)

    def run(self, cmd):
        self.t.write(cmd.encode() + b"\n")
        out = self.t.read_until(MARKER, timeout=120).decode(errors="replace")
        return out.split("\n", 1)[1].removesuffix(MARKER.decode()).strip()


def fetch(remote):
    ftp = ftplib.FTP(HOST, timeout=30)
    ftp.login()
    buf = bytearray()
    ftp.retrbinary(f"RETR {remote}", buf.extend, blocksize=65536)
    ftp.quit()
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("device")
    ap.add_argument("local")
    ap.add_argument("--mib", type=int, required=True, help="MiB to dump from the start")
    args = ap.parse_args()

    sh = Shell()
    whole = hashlib.md5()
    with open(args.local, "wb") as f:
        for skip in range(0, args.mib, CHUNK_MIB):
            count = min(CHUNK_MIB, args.mib - skip)
            sh.run(f"dd if={args.device} of={REMOTE} bs=1M skip={skip} count={count} 2>/dev/null")
            remote_md5 = sh.run(f"md5sum {REMOTE}").split()[0]
            data = fetch(REMOTE)
            local_md5 = hashlib.md5(data).hexdigest()
            if local_md5 != remote_md5:
                raise SystemExit(f"MD5 mismatch at {skip} MiB: {local_md5} != {remote_md5}")
            f.write(data)
            whole.update(data)
            print(f"{args.device} {skip}-{skip + count} MiB ok ({len(data)} bytes)")
    sh.run(f"rm -f {REMOTE}")
    remote_whole = sh.run(f"dd if={args.device} bs=1M count={args.mib} 2>/dev/null | md5sum").split()[0]
    print(f"whole: local {whole.hexdigest()} remote {remote_whole}",
          "MATCH" if whole.hexdigest() == remote_whole else "MISMATCH")


if __name__ == "__main__":
    main()
