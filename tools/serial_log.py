#!/usr/bin/env python3
"""Capture the Clip's debug serial (DBGU, 115200 8N1) to a log file and stdout.

    .venv/bin/python -I -B tools/serial_log.py --seconds 60            # auto-detects /dev/ttyUSB*
    .venv/bin/python -I -B tools/serial_log.py --port /dev/ttyUSB0 --out analysis/serial.log
"""
import argparse
import glob
import os
import sys
import time

import serial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="default: first /dev/ttyUSB* or /dev/ttyACM*")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--out", default=os.path.join(ROOT, "analysis",
                                                  time.strftime("serial-%Y%m%d-%H%M%S.log")))
    args = ap.parse_args()

    port = args.port or next(iter(sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))), None)
    if not port:
        sys.exit("error: no /dev/ttyUSB* or /dev/ttyACM* (is the USB serial adapter plugged in?)")
    total = 0
    end = time.time() + args.seconds
    with serial.Serial(port, args.baud, timeout=0.5) as ser, open(args.out, "wb") as f:
        print(f"capturing {port} at {args.baud} for {args.seconds:.0f} s -> {args.out}", flush=True)
        while time.time() < end:
            data = ser.read(4096)
            if data:
                f.write(data)
                f.flush()
                total += len(data)
                sys.stdout.write(data.decode("ascii", "replace"))
                sys.stdout.flush()
    print(f"\n[{total} bytes captured]", flush=True)


if __name__ == "__main__":
    main()
