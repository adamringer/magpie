#!/usr/bin/env python3
"""Host client for the Clip's USB data interface (protocol v1, see docs/PHONE_USB.md).

    python3 -I tools/phone/usbclient.py status
    python3 -I tools/phone/usbclient.py list --max 50
    python3 -I tools/phone/usbclient.py get 20261031-101500.jpg --dest ~/clip
    python3 -I tools/phone/usbclient.py get --all --dest ~/clip
    python3 -I tools/phone/usbclient.py hash 20261031-101500.jpg
    python3 -I tools/phone/usbclient.py delete 20261031-101500.jpg --yes
    python3 -I tools/phone/usbclient.py time          # Clip-minus-host clock offset, nothing is set
    python3 -I tools/phone/usbclient.py settime       # set the Clip's clocks from this host
    python3 -I tools/phone/usbclient.py bench 20261031-101500.jpg
    python3 -I tools/phone/usbclient.py --json status # raw JSON instead of text

The Clip shows up as 0525:a4a1 with its CDC-ECM network interfaces plus one vendor-specific interface (class
0xFF) with a bulk OUT and a bulk IN endpoint. Only that interface is claimed; the network interfaces are never
touched or detached, so the ECM link keeps working while this runs. Needs pyusb (the system package python3-usb
is enough; it is imported only when a command actually talks to the device, so --help works without it) and
permission to open the USB device (root or a udev rule).

`delete` never deletes blindly: it HASHes every named file on the Clip and sends DELETE with exactly those
hashes, so a file is removed only if it is still the one that was hashed. Run `get` first if you want a copy.

The Client class is also the reference for the browser client: header packing, reassembly by `len`, the
status word, and the clock-sync arithmetic are all in it.

Transfers are one request, one complete response, no pipelining. If a response has the wrong magic, command or
seq, or does not arrive in time, the stream can no longer be trusted: a ProtocolError / TimeoutError is
raised and the Client should be discarded (reconnect).
"""
import argparse
import errno
import hashlib
import json
import os
import re
import struct
import sys
import time

VID = 0x0525
PID = 0xA4A1

MAGIC = 0x3150474D  # "MGP1" little-endian
HDR = struct.Struct("<IHHII")  # magic, cmd, flags, seq, len
HDR_LEN = HDR.size  # 16
RESP_FLAG = 0x8000
MAX_PAYLOAD = 65536
MAX_READ = MAX_PAYLOAD - 4  # READ result is preceded by the 4-byte status
MAX_LIST = 500
MAX_DELETE = 500

CMD_STATUS = 0x0001
CMD_LIST = 0x0002
CMD_READ = 0x0003
CMD_HASH = 0x0004
CMD_DELETE = 0x0005
CMD_TIME = 0x0006
CMD_SETTIME = 0x0007

NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}\.(jpg|snap|snap\.time)$")


class UsbClientError(Exception):
    pass


class ProtocolError(UsbClientError):
    """The response did not look like the protocol says it must."""


class DeviceError(UsbClientError):
    """The Clip answered with a negative status."""

    def __init__(self, status, cmd=None):
        self.status = status
        self.cmd = cmd
        self.errno_name = errno.errorcode.get(-status, "E%d" % -status)
        what = "command 0x%04x" % cmd if cmd is not None else "command"
        super().__init__("%s failed: status %d (%s: %s)" % (what, status, self.errno_name, os.strerror(-status)))


class Transport:
    """Byte pipe to the interface's two bulk endpoints.

    write(data): send all of `data` on bulk OUT.
    read(maxlen, timeout_ms): one read from bulk IN. Returns the bytes of the next packet(s), at least one
    byte, or b"" for a zero-length packet. The transport may return more than maxlen (a read is rounded up to
    whole USB packets), never less than what was available up to that rounding. Raises TimeoutError when
    nothing arrives in time.
    """

    def write(self, data):
        raise NotImplementedError

    def read(self, maxlen, timeout_ms):
        raise NotImplementedError

    def close(self):
        pass


class UsbTransport(Transport):
    """pyusb transport for the vendor-specific (class 0xFF) interface of 0525:a4a1."""

    def __init__(self, vid=VID, pid=PID, write_timeout_ms=5000):
        try:
            import usb.core  # lazy: pyusb is not needed for --help or the tests
            import usb.util
        except ImportError as e:
            raise UsbClientError("pyusb is not installed (system package python3-usb)") from e

        self._usb = usb
        self._write_timeout = write_timeout_ms
        self._dev = None
        self._intf = None
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise UsbClientError("USB device %04x:%04x not found" % (vid, pid))
        cfg = dev.get_active_configuration()
        found = None
        for intf in cfg:
            if intf.bInterfaceClass != 0xFF:
                continue
            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
            )
            ep_in = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
            )
            if ep_out is not None and ep_in is not None:
                found = (intf, ep_out, ep_in)
                break
        if found is None:
            raise UsbClientError("no vendor-specific interface with bulk IN and OUT endpoints on %04x:%04x" % (vid, pid))
        self._intf, self._ep_out, self._ep_in = found
        usb.util.claim_interface(dev, self._intf.bInterfaceNumber)  # only this interface; no detach, no reset
        self._dev = dev

    def write(self, data):
        try:
            self._ep_out.write(bytes(data), self._write_timeout)
        except self._usb.core.USBTimeoutError as e:
            raise TimeoutError("USB write timed out") from e

    def read(self, maxlen, timeout_ms):
        mps = self._ep_in.wMaxPacketSize
        size = max(mps, -(-maxlen // mps) * mps)  # whole packets, so a full packet never overflows the buffer
        try:
            return bytes(self._ep_in.read(size, timeout_ms))
        except self._usb.core.USBTimeoutError as e:
            raise TimeoutError("USB read timed out") from e

    def close(self):
        if self._dev is not None:
            try:
                self._usb.util.release_interface(self._dev, self._intf.bInterfaceNumber)
            finally:
                self._usb.util.dispose_resources(self._dev)
                self._dev = None


class Client:
    """Framing plus one method per command. `wall_ns` / `mono_ns` are injectable clocks (for tests)."""

    def __init__(self, transport, timeout_ms=5000, seq=1, wall_ns=time.time_ns, mono_ns=time.monotonic_ns):
        self.t = transport
        self.timeout_ms = timeout_ms
        self.seq = seq & 0xFFFFFFFF
        self.wall_ns = wall_ns
        self.mono_ns = mono_ns

    # -- framing ---------------------------------------------------------------------------------------------

    def _read_exact_response(self):
        """Reassemble one response: header, then `len` more bytes. Returns (cmd, seq, payload)."""
        deadline = self.mono_ns() + self.timeout_ms * 1_000_000

        def more(need):
            left_ms = (deadline - self.mono_ns()) // 1_000_000
            if left_ms <= 0:
                raise TimeoutError("response timed out")
            return self.t.read(need, int(left_ms))

        buf = bytearray()
        while len(buf) < HDR_LEN:
            buf += more(HDR_LEN - len(buf))
        magic, cmd, _flags, seq, length = HDR.unpack_from(buf)
        if magic != MAGIC:
            raise ProtocolError("bad response magic 0x%08x" % magic)
        if length > MAX_PAYLOAD:
            raise ProtocolError("response len %d exceeds %d" % (length, MAX_PAYLOAD))
        total = HDR_LEN + length
        while len(buf) < total:
            buf += more(total - len(buf))
        if len(buf) > total:
            raise ProtocolError("response has %d bytes after its len" % (len(buf) - total))
        return cmd, seq, bytes(buf[HDR_LEN:])

    def request(self, cmd, payload=b""):
        """Send one request and return the result bytes after the status. Raises DeviceError on status < 0."""
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("request payload %d bytes exceeds %d" % (len(payload), MAX_PAYLOAD))
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        self.t.write(HDR.pack(MAGIC, cmd, 0, seq, len(payload)) + payload)
        rcmd, rseq, body = self._read_exact_response()
        if rcmd != (cmd | RESP_FLAG):
            raise ProtocolError("response cmd 0x%04x, expected 0x%04x" % (rcmd, cmd | RESP_FLAG))
        if rseq != seq:
            raise ProtocolError("response seq %d, expected %d" % (rseq, seq))
        if len(body) < 4:
            raise ProtocolError("response payload of %d bytes has no status word" % len(body))
        (status,) = struct.unpack_from("<i", body)
        if status < 0:
            raise DeviceError(status, cmd)
        if status > 0:
            raise ProtocolError("positive status %d" % status)
        return body[4:]

    def _json_request(self, cmd, obj=None):
        payload = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode("utf-8")
        raw = self.request(cmd, payload)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise ProtocolError("result is not valid JSON: %s" % e) from e

    # -- commands --------------------------------------------------------------------------------------------

    def status(self):
        return self._json_request(CMD_STATUS)

    def list(self, after="", max=200):
        return self._json_request(CMD_LIST, {"after": after, "max": max})

    def read(self, name, offset, length):
        data = self.request(CMD_READ, json.dumps({"name": name, "offset": offset, "len": length}, separators=(",", ":")).encode())
        if len(data) > length:
            raise ProtocolError("READ returned %d bytes, asked for %d" % (len(data), length))
        return data

    def hash(self, name):
        return self._json_request(CMD_HASH, {"name": name})

    def delete(self, files):
        """files: list of {"name": ..., "sha256": ...} (at most 500)."""
        return self._json_request(CMD_DELETE, {"files": list(files)})

    def time(self):
        return self._json_request(CMD_TIME)

    def settime(self, utc_ms):
        return self._json_request(CMD_SETTIME, {"utc_ms": int(utc_ms)})

    # -- helpers ---------------------------------------------------------------------------------------------

    def list_all(self, page=MAX_LIST):
        files = []
        after = ""
        while True:
            r = self.list(after, page)
            got = r["files"]
            files.extend(got)
            if not r["more"]:
                return files
            if not got:
                raise ProtocolError("LIST says more but returned an empty page")
            if got[-1]["name"] <= after:
                raise ProtocolError("LIST page is not ascending after %r" % after)
            after = got[-1]["name"]

    def _download_one(self, name, dest_dir, progress=None):
        res = {"name": name, "ok": False, "size": 0, "sha256": None, "device_sha256": None, "error": None}
        if not NAME_RE.match(name):
            res["error"] = "invalid file name"
            return res
        final = os.path.join(dest_dir, name)
        part = final + ".part"
        h = hashlib.sha256()
        size = 0
        try:
            with open(part, "wb") as f:
                while True:
                    data = self.read(name, size, MAX_READ)
                    if not data:
                        break
                    f.write(data)
                    h.update(data)
                    size += len(data)
                    if progress:
                        progress(name, size)
            res["size"] = size
            res["sha256"] = h.hexdigest()
            dev = self.hash(name)
            res["device_sha256"] = dev["sha256"]
            if dev["sha256"] != res["sha256"] or dev["size"] != size:
                res["error"] = "hash mismatch: device %s (%d bytes), received %s (%d bytes)" % (
                    dev["sha256"], dev["size"], res["sha256"], size)
            else:
                os.replace(part, final)
                res["ok"] = True
        except DeviceError as e:
            res["error"] = str(e)
        finally:
            if not res["ok"] and os.path.exists(part):
                os.unlink(part)  # a failed download leaves nothing behind
        return res

    def download(self, names, dest_dir, progress=None):
        """READ each file in 64 KiB chunks into <name>.part, check it against the device's HASH, rename on
        success. Returns one dict per file: name, ok, size, sha256 (local), device_sha256, error."""
        os.makedirs(dest_dir, exist_ok=True)
        return [self._download_one(n, dest_dir, progress) for n in names]

    def delete_verified(self, names):
        """HASH each file on the device, then DELETE with exactly those hashes. Returns one dict per name:
        name, ok, err, sha256."""
        out = {}
        todo = []
        for n in names:
            try:
                todo.append({"name": n, "sha256": self.hash(n)["sha256"]})
            except DeviceError as e:
                out[n] = {"name": n, "ok": False, "err": str(e), "sha256": None}
        for i in range(0, len(todo), MAX_DELETE):
            batch = todo[i:i + MAX_DELETE]
            hashes = {f["name"]: f["sha256"] for f in batch}
            for r in self.delete(batch)["results"]:
                out[r["name"]] = {"name": r["name"], "ok": bool(r["ok"]), "err": r.get("err"), "sha256": hashes.get(r["name"])}
        return [out[n] for n in names if n in out]

    def time_sample(self):
        """One TIME round trip: host wall time at send (ms), round trip (ms), the reply, and the estimated
        Clip-minus-host offset (ms), assuming the Clip sampled its clock halfway through the round trip."""
        w0 = self.wall_ns()
        m0 = self.mono_ns()
        r = self.time()
        rtt_ns = self.mono_ns() - m0
        rtt_ms = rtt_ns / 1e6
        host_mid_ms = w0 / 1e6 + rtt_ms / 2
        return {"host_send_ms": w0 / 1e6, "rtt_ms": rtt_ms, "clip_utc_ms": r["clip_utc_ms"],
                "offset_ms": r["clip_utc_ms"] - host_mid_ms, "reply": r}

    def measure_offset(self, rounds=8):
        """TIME `rounds` times; the sample with the smallest round trip gives the offset estimate. The Clip's
        clock has 1 ms resolution, so the estimate is good to about +-(rtt/2 + 0.5) ms."""
        samples = [self.time_sample() for _ in range(max(1, rounds))]
        best = min(samples, key=lambda s: s["rtt_ms"])
        return {"rounds": len(samples), "best_rtt_ms": best["rtt_ms"], "offset_ms": best["offset_ms"],
                "samples": [{"rtt_ms": s["rtt_ms"], "offset_ms": s["offset_ms"]} for s in samples], "best": best}

    def sync_clock(self, rounds=8):
        """Measure the round trip with `rounds` TIME requests, then SETTIME with utc_ms = host time at send
        plus half the smallest round trip (the Clip processes the request about then)."""
        m = self.measure_offset(rounds)
        utc_ms = round(self.wall_ns() / 1e6 + m["best_rtt_ms"] / 2)
        reply = self.settime(utc_ms)
        return {"rounds": m["rounds"], "best_rtt_ms": m["best_rtt_ms"], "offset_before_ms": m["offset_ms"],
                "sent_utc_ms": utc_ms, "reply": reply}

    def bench(self, name, chunk=MAX_READ, max_bytes=None):
        """Read a file (or its first max_bytes) and report the throughput."""
        total = 0
        t0 = self.mono_ns()
        while max_bytes is None or total < max_bytes:
            want = chunk if max_bytes is None else min(chunk, max_bytes - total)
            data = self.read(name, total, want)
            if not data:
                break
            total += len(data)
        secs = (self.mono_ns() - t0) / 1e9
        return {"name": name, "bytes": total, "seconds": secs, "mib_per_s": total / secs / 1048576 if secs > 0 else None}


# -- CLI ---------------------------------------------------------------------------------------------------


def _emit(args, obj, text_lines):
    if args.json:
        print(json.dumps(obj, indent=2))
    else:
        for line in text_lines:
            print(line)


def _fmt_ms(v):
    return "%+.1f ms" % v


def run(args, c):
    cmd = args.cmd
    if cmd == "status":
        r = c.status()
        _emit(args, r, ["%s: %s" % (k, v) for k, v in r.items()])
    elif cmd == "list":
        r = c.list(args.after, args.max)
        lines = ["%s  %d bytes  mtime %s" % (f["name"], f["size"], f["mtime"]) for f in r["files"]]
        lines.append("%d files%s" % (len(r["files"]), ", more available (use --after %s)" % r["files"][-1]["name"]
                                       if r["more"] and r["files"] else ""))
        _emit(args, r, lines)
    elif cmd == "get":
        names = [f["name"] for f in c.list_all()] if args.all else args.names
        rs = c.download(names, args.dest)
        lines = []
        for r in rs:
            lines.append("%s  %s  %d bytes  sha256 %s%s" % ("ok  " if r["ok"] else "FAIL", r["name"], r["size"],
                                                           r["sha256"], "" if r["ok"] else "  (" + r["error"] + ")"))
        lines.append("%d of %d files downloaded" % (sum(r["ok"] for r in rs), len(rs)))
        _emit(args, rs, lines)
        return 0 if all(r["ok"] for r in rs) else 1
    elif cmd == "hash":
        r = c.hash(args.name)
        _emit(args, r, ["%s  %d bytes  sha256 %s" % (r["name"], r["size"], r["sha256"])])
    elif cmd == "delete":
        rs = c.delete_verified(args.names)
        lines = ["%s  %s%s" % ("deleted" if r["ok"] else "NOT DELETED", r["name"], "" if r["ok"] else "  (%s)" % r["err"])
                 for r in rs]
        _emit(args, rs, lines)
        return 0 if len(rs) == len(args.names) and all(r["ok"] for r in rs) else 1
    elif cmd == "time":
        m = c.measure_offset(args.rounds)
        out = {k: m[k] for k in ("rounds", "best_rtt_ms", "offset_ms", "samples")}
        out["clip_utc_ms"] = m["best"]["clip_utc_ms"]
        _emit(args, out, ["Clip minus host: %s (best of %d, round trip %.2f ms)" % (
            _fmt_ms(m["offset_ms"]), m["rounds"], m["best_rtt_ms"])])
    elif cmd == "settime":
        r = c.sync_clock(args.rounds)
        rep = r["reply"]
        _emit(args, r, [
            "offset before: %s (best of %d, round trip %.2f ms)" % (_fmt_ms(r["offset_before_ms"]), r["rounds"], r["best_rtt_ms"]),
            "sent utc_ms %d" % r["sent_utc_ms"],
            "Clip clock before %s, after %s ms, rtc %s, rtt_cal %s" % (rep.get("before_ms"), rep.get("after_ms"),
                                                                       rep.get("rtc"), rep.get("rtt_cal")),
        ])
        return 0 if rep.get("rtc") == "ok" else 1
    elif cmd == "bench":
        r = c.bench(args.name, max_bytes=args.max_bytes)
        _emit(args, r, ["%s: %d bytes in %.3f s = %s" % (
            r["name"], r["bytes"], r["seconds"], "%.2f MiB/s" % r["mib_per_s"] if r["mib_per_s"] else "n/a")])
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Host client for the Clip's USB data interface (docs/PHONE_USB.md).")
    p.add_argument("--json", action="store_true", help="print raw JSON results")
    p.add_argument("--timeout", type=float, default=5.0, help="per-response timeout in seconds (default 5)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print raw JSON results")
        return sp

    add("status", "daemon version, clock, file counts, battery")
    sp = add("list", "list one page of files")
    sp.add_argument("--after", default="", help="list names strictly after this one")
    sp.add_argument("--max", type=int, default=200, help="page size, at most 500 (default 200)")
    sp = add("get", "download files and verify each against the Clip's sha256")
    sp.add_argument("names", nargs="*", metavar="NAME")
    sp.add_argument("--all", action="store_true", help="download every file on the Clip")
    sp.add_argument("--dest", required=True, help="destination directory")
    sp = add("hash", "sha256 of a file on the Clip")
    sp.add_argument("name", metavar="NAME")
    sp = add("delete", "delete files on the Clip, each only if it still matches the hash just taken")
    sp.add_argument("names", nargs="+", metavar="NAME")
    sp.add_argument("--yes", action="store_true", help="really delete (required)")
    sp = add("time", "estimate the Clip-minus-host clock offset (sets nothing)")
    sp.add_argument("--rounds", type=int, default=8)
    sp = add("settime", "set the Clip's clock from this host")
    sp.add_argument("--rounds", type=int, default=8)
    sp = add("bench", "read throughput on one file")
    sp.add_argument("name", metavar="NAME")
    sp.add_argument("--max-bytes", type=int, default=None, help="stop after this many bytes")
    return p


def main(argv=None, transport=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "get" and bool(args.names) == bool(args.all):
        print("get: give file names or --all, not both or neither", file=sys.stderr)
        return 2
    if args.cmd == "delete" and not args.yes:
        print("delete: refusing without --yes (nothing was contacted or deleted)", file=sys.stderr)
        return 2
    own = transport is None
    try:
        if own:
            transport = UsbTransport()
        c = Client(transport, timeout_ms=int(args.timeout * 1000))
        return run(args, c)
    except (UsbClientError, TimeoutError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    finally:
        if own and transport is not None:
            transport.close()


if __name__ == "__main__":
    sys.exit(main())
