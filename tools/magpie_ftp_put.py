#!/usr/bin/env python3
"""Upload a file to the Clip's ftpd (root, writable).

    .venv/bin/python -I tools/magpie_ftp_put.py local_file.txt /tmp/remote_file.txt
"""
import argparse
import ftplib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("local")
    ap.add_argument("remote")
    args = ap.parse_args()

    ftp = ftplib.FTP("192.168.2.2", timeout=30)
    ftp.login()
    with open(args.local, "rb") as f:
        ftp.storbinary(f"STOR {args.remote}", f, blocksize=65536)
    ftp.quit()
    print(f"{args.local} -> {args.remote}")


if __name__ == "__main__":
    main()
