#!/usr/bin/env python3
"""Set the docked Clip's clocks from this (NTP-synchronised) host, and give the RTT a known epoch.

    .venv/bin/python -I -W ignore tools/magpie_timesync.py [--check]

1. System clock: `date -s` sent so that it runs on the Clip at a whole second of this host's clock (send time
   corrected by the measured one-way latency), then `hwclock -w -u` (the RTC keeps whole seconds only).
2. RTT (Real-time Timer, backup power domain, survives fast-resume power cuts and cold boots): only if it is
   not already running at our prescaler (reset default after a total power loss), restart it with 32 slow-clock
   cycles = 0.9765625 ms per count (32-bit counter, wraps after ~48 days) and append its epoch to
   /data/time/rtt_epoch. Otherwise it is left running, so earlier captures keep their timebase.
3. Calibration: the RTT count is sampled many times over ~10 s; the samples with the shortest round trip give
   the host time of count 0. A fresh restart writes it as `epoch_utc`; a running RTT gets a line in
   /data/time/rtt_cal (fitted epoch and its difference to the stored one = crystal drift since then).
4. Check: the Clip's next second rollover is timed from this host to report the remaining system-clock offset.
--check skips steps 1-3 and only measures the offsets (system clock, and the RTT against the stored epoch).
--wait retries the connection for up to 120 s (for the docking hook, while the Clip boots).

gpsnap writes the RTT count of each capture into its .time file; UTC = epoch_utc + count * pres / 32768
(plus the crystal's ppm, which the position solver can fit).
"""
import argparse
import datetime
import re
import sys
import telnetlib
import time

HOST = "192.168.2.2"
MARKER = b"__TS_PROMPT__# "
SLCK = 32768.0
PRES = 32


class Clip:
    def __init__(self, wait=0):
        end = time.time() + wait
        while True:
            try:
                self.t = telnetlib.Telnet(HOST, 23, timeout=10)
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(3)
        self.t.read_until(b"# ", timeout=10)
        self.t.write(b"PS1='" + MARKER + b"'\n")
        self.t.read_until(MARKER, timeout=10)
        self.t.read_until(MARKER, timeout=10)

    def run(self, cmd, timeout=30):
        """returns (output, host time just before sending, host time when the prompt came back)"""
        t0 = time.time()
        self.t.write(cmd.encode() + b"\n")
        out = self.t.read_until(MARKER, timeout=timeout).decode(errors="replace")
        t1 = time.time()
        body = out.split("\n", 1)[1] if "\n" in out else ""
        return body.removesuffix(MARKER.decode()).replace("\r", "").strip(), t0, t1


def latency(c, n=15):
    rt = sorted(t1 - t0 for _, t0, t1 in (c.run("true") for _ in range(n)))
    return rt[0] / 2, rt[len(rt) // 2]


def rtt_samples(c, n=40):
    s = []
    for _ in range(n):
        out, t0, t1 = c.run("devmem 0xfffffe28 32")
        s.append((t1 - t0, (t0 + t1) / 2, int(out.split()[-1], 16)))
        time.sleep(0.2)
    return s


def epoch_from(samples, pres):
    best = sorted(samples)[: max(5, len(samples) // 4)]          # shortest round trips
    tick = pres / SLCK
    return sum(mid - v * tick for _, mid, v in best) / len(best), best[0][0]


def second_offset(c):
    """Clip system clock minus host clock (s), from the time its seconds counter rolls over."""
    lat, _ = latency(c, 9)
    out, t0, t1 = c.run('t=$(date +%s); while [ "$(date +%s)" = "$t" ]; do :; done; date +%s')
    clip_sec = int(out.split()[-1])
    # the rollover was seen just before the reply left the Clip: ~ t1 - lat, minus one `date` (a few ms)
    return clip_sec - (t1 - lat), lat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only measure the offsets")
    ap.add_argument("--wait", action="store_true", help="retry the connection for up to 120 s")
    a = ap.parse_args()
    c = Clip(120 if a.wait else 0)
    print("== %s" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    lat, med = latency(c)
    print("one-way latency %.1f ms (median round trip %.1f ms)" % (lat * 1e3, med * 1e3))

    if not a.check:
        # system clock: run `date -s` at a whole second of this host's clock; the second pass sends it earlier
        # by the offset the first one left (busybox start-up time on the Clip, ~0.1 s)
        lead = 0.0
        for _ in range(2):
            target = int(time.time()) + 2
            stamp = datetime.datetime.fromtimestamp(target, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            time.sleep(max(0.0, target - lat - lead - time.time()))
            c.run('date -u -s "%s" >/dev/null' % stamp)
            off, _ = second_offset(c)
            print("  set pass: offset %+.1f ms" % (off * 1e3))
            lead -= off
        c.run("hwclock -w -u")
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mr, _, _ = c.run("devmem 0xfffffe20 32")
        stored, _, _ = c.run("tail -n 1 /data/time/rtt_epoch 2>/dev/null")
        m = re.search(r"epoch_utc=([\d.]+) pres=(\d+)", stored)
        if (int(mr.split()[-1], 16) & 0xFFFF) != PRES or not m:
            # not ours (power loss or first use): restart with our prescaler (RTTRST, bit 18) and fit the epoch
            c.run("devmem 0xfffffe20 32 0x%08x" % (0x40000 | PRES))
            epoch, rtmin = epoch_from(rtt_samples(c), PRES)
            line = "epoch_utc=%.4f pres=%d set=%s fit_roundtrip_ms=%.1f" % (epoch, PRES, now, rtmin * 1e3)
            c.run("mkdir -p /data/time && echo '%s' >> /data/time/rtt_epoch && sync" % line)
            print("RTT restarted:", line)
        else:
            # running: keep it, record a calibration point (drift of the 32 kHz crystal since the epoch was set)
            ep = float(m.group(1))
            fit, rtmin = epoch_from(rtt_samples(c), PRES)
            line = "cal=%s epoch_fit=%.4f diff_ms=%+.1f fit_roundtrip_ms=%.1f" % (now, fit, (fit - ep) * 1e3, rtmin * 1e3)
            c.run("echo '%s' >> /data/time/rtt_cal && sync" % line)
            print("RTT kept, calibration:", line)

    off, _ = second_offset(c)
    print("Clip system clock offset: %+.1f ms (Clip minus host)" % (off * 1e3))
    out, _, _ = c.run("tail -n 1 /data/time/rtt_epoch 2>/dev/null")
    m = re.search(r"epoch_utc=([\d.]+) pres=(\d+)", out)
    if m:
        ep, pres = float(m.group(1)), int(m.group(2))
        s = rtt_samples(c, 20)
        now_ep, _ = epoch_from(s, pres)
        print("RTT vs stored epoch: %+.1f ms" % ((now_ep - ep) * 1e3))
    c.t.write(b"exit\n")


if __name__ == "__main__":
    sys.exit(main())
