#!/usr/bin/env python3
"""Fetch a file (or the first N bytes of a device node) from the Clip's ftpd.

    .venv/bin/python -I tools/magpie_ftp_get.py /dev/mmcblk0 backup/mmcblk0_first32M.bin --limit 33554432
"""
import argparse
import ftplib


class Done(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("remote")
    ap.add_argument("local")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many bytes")
    args = ap.parse_args()

    ftp = ftplib.FTP("192.168.2.2", timeout=30)
    ftp.login()
    got = 0
    with open(args.local, "wb") as f:
        def sink(chunk):
            nonlocal got
            if args.limit:
                chunk = chunk[: args.limit - got]
            f.write(chunk)
            got += len(chunk)
            if args.limit and got >= args.limit:
                raise Done
        try:
            ftp.retrbinary(f"RETR {args.remote}", sink, blocksize=65536)
        except Done:
            ftp.close()
            print(f"{args.remote}: stopped at {got} bytes")
            return
    ftp.quit()
    print(f"{args.remote}: {got} bytes")


if __name__ == "__main__":
    main()
