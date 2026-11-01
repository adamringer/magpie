"""tools/phone/usbclient.py against an in-memory fake of the device side of docs/PHONE_USB.md (no USB needed)."""
import hashlib
import importlib.util
import json
import pathlib
import struct

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("usbclient", ROOT / "tools" / "phone" / "usbclient.py")
uc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uc)

PACKET = 512
EINVAL, ENOENT = 22, 2


class VClock:
    """Virtual time shared by the client (as its clocks) and the fake device. Nanoseconds."""

    def __init__(self, wall_ns=1_800_000_000_000_000_000):
        self.ns = 0
        self.wall0 = wall_ns

    def mono(self):
        return self.ns

    def wall(self):
        return self.wall0 + self.ns

    def advance_ms(self, ms):
        self.ns += int(ms * 1e6)


class FakeDevice(uc.Transport):
    """Device side of the protocol over a dict of files. Responses are delivered in 512-byte packets."""

    def __init__(self, files=None, clock=None, clip_offset_ms=0):
        self.files = dict(files or {})
        self.clock = clock or VClock()
        self.clip_offset_ms = clip_offset_ms  # Clip clock minus host clock
        self.pending = bytearray()
        self.requests = []  # (cmd, seq, payload)
        self.corrupt_once = set()  # (name, offset): READ returns flipped bytes once
        self.tamper = None  # callable(response bytes) -> bytes
        self.time_delays = []  # per TIME request: (ms to reach the device, ms back)
        self.other_delay = (0.0, 0.0)
        self.back_delay_ms = 0.0
        self.settime_host_ms = None  # host virtual wall time (ms) when SETTIME was processed
        self.settime_utc_ms = None

    # -- transport -------------------------------------------------------------------------------------------
    def write(self, data):
        data = bytes(data)
        assert len(data) >= uc.HDR_LEN
        magic, cmd, flags, seq, length = uc.HDR.unpack_from(data)
        payload = data[uc.HDR_LEN:]
        assert len(payload) == length
        self.requests.append((cmd, seq, payload))
        if cmd == uc.CMD_TIME and self.time_delays:
            out_ms, back_ms = self.time_delays.pop(0)
        else:
            out_ms, back_ms = self.other_delay
        self.clock.advance_ms(out_ms)
        resp = self.handle(magic, cmd, seq, length, payload)
        if self.tamper:
            resp = self.tamper(resp)
        self.back_delay_ms = back_ms
        self.pending += resp

    def read(self, maxlen, timeout_ms):
        if not self.pending:
            raise TimeoutError("fake: nothing to read")
        if self.back_delay_ms:
            self.clock.advance_ms(self.back_delay_ms)
            self.back_delay_ms = 0
        n = min(PACKET, len(self.pending))  # one packet per read, regardless of maxlen
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out

    # -- protocol --------------------------------------------------------------------------------------------
    @staticmethod
    def frame(cmd, seq, status, result=b""):
        body = struct.pack("<i", status) + result
        return uc.HDR.pack(uc.MAGIC, cmd | 0x8000, 0, seq, len(body)) + body

    def now_ms(self):
        return self.clock.wall() // 1_000_000 + self.clip_offset_ms

    def handle(self, magic, cmd, seq, length, payload):
        if magic != uc.MAGIC or length > 65536 or cmd not in range(1, 8):
            return self.frame(cmd, seq, -EINVAL)
        try:
            req = json.loads(payload) if payload else None
            fn = getattr(self, "cmd_%d" % cmd)
            st, res = fn(req)
        except (ValueError, KeyError, TypeError):
            return self.frame(cmd, seq, -EINVAL)
        if isinstance(res, (dict, list)):
            res = json.dumps(res).encode()
        return self.frame(cmd, seq, st, res)

    def valid(self, name):
        return isinstance(name, str) and uc.NAME_RE.match(name) is not None

    def cmd_1(self, req):
        return 0, {"proto": 1, "version": "fake", "clip_utc_ms": self.now_ms(), "uptime_s": 12.5, "rtt": None,
                   "photos": sum(n.endswith(".jpg") for n in self.files), "snaps": 0, "snap_times": 0,
                   "bytes": sum(len(v) for v in self.files.values()), "free_kb": 1000, "battery_raw": None,
                   "charging": None}

    def cmd_2(self, req):
        after, mx = req.get("after", ""), req.get("max", 200)
        if not 1 <= mx <= 500:
            return -EINVAL, b""
        names = [n for n in sorted(self.files) if n > after]
        page = names[:mx]
        return 0, {"files": [{"name": n, "size": len(self.files[n]), "mtime": 1700000000} for n in page],
                   "more": len(names) > mx}

    def cmd_3(self, req):
        name, off, ln = req["name"], req["offset"], req["len"]
        if not self.valid(name) or ln > 65536 - 4 or off < 0 or ln < 0:
            return -EINVAL, b""
        if name not in self.files:
            return -ENOENT, b""
        data = self.files[name][off:off + ln]
        if (name, off) in self.corrupt_once and data:
            self.corrupt_once.discard((name, off))
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return 0, data

    def cmd_4(self, req):
        name = req["name"]
        if not self.valid(name):
            return -EINVAL, b""
        if name not in self.files:
            return -ENOENT, b""
        d = self.files[name]
        return 0, {"name": name, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}

    def cmd_5(self, req):
        files = req["files"]
        if len(files) > 500:
            return -EINVAL, b""
        results = []
        for f in files:
            n = f["name"]
            if not self.valid(n):
                results.append({"name": n, "ok": False, "err": "EINVAL"})
            elif n not in self.files:
                results.append({"name": n, "ok": False, "err": "ENOENT"})
            elif hashlib.sha256(self.files[n]).hexdigest() != f["sha256"]:
                results.append({"name": n, "ok": False, "err": "hash mismatch"})
            else:
                del self.files[n]
                results.append({"name": n, "ok": True})
        return 0, {"results": results}

    def cmd_6(self, req):
        return 0, {"clip_utc_ms": self.now_ms(), "mono_ns": self.clock.ns, "rtt": None}

    def cmd_7(self, req):
        before = self.now_ms()
        self.settime_host_ms = self.clock.wall() / 1e6
        self.settime_utc_ms = req["utc_ms"]
        self.clip_offset_ms = req["utc_ms"] - self.clock.wall() // 1_000_000
        return 0, {"before_ms": before, "after_ms": self.now_ms(), "rtc": "ok", "rtt_cal": None}


def make_files(n=5, size=3000):
    return {"20261031-1015%02d.jpg" % i: bytes((i * 7 + j) % 251 for j in range(size + i * 777)) for i in range(n)}


def mk(files=None, **kw):
    clock = VClock()
    dev = FakeDevice(files if files is not None else make_files(), clock=clock, **kw)
    return dev, uc.Client(dev, wall_ns=clock.wall, mono_ns=clock.mono)


# -- framing ---------------------------------------------------------------------------------------------------


def test_header_layout():
    assert uc.HDR.size == 16
    assert uc.HDR.pack(uc.MAGIC, 1, 0, 2, 3)[:4] == b"MGP1"


def test_framing_round_trip_and_seq_echo():
    dev, c = mk()
    r1 = c.status()
    r2 = c.status()
    assert r1["proto"] == 1 and r2["version"] == "fake"
    assert [(cmd, seq) for cmd, seq, _ in dev.requests] == [(1, 1), (1, 2)]
    assert c.seq == 3


def test_seq_wraps():
    dev, c = mk()
    c.seq = 0xFFFFFFFF
    c.status()
    c.status()
    assert [seq for _, seq, _ in dev.requests] == [0xFFFFFFFF, 0]


def test_large_response_is_reassembled_from_packets():
    files = {"20261031-101500.jpg": bytes(range(256)) * 300}  # 76800 bytes
    dev, c = mk(files)
    data = c.read("20261031-101500.jpg", 0, 65532)
    assert data == files["20261031-101500.jpg"][:65532]


@pytest.mark.parametrize("size", [PACKET - 20, PACKET - 19, 2 * PACKET - 20, 100])  # incl. exact packet multiples
def test_read_sizes_around_packet_boundaries(size):
    n = "20261031-101500.jpg"
    dev, c = mk({n: bytes(range(256)) * 10})
    assert c.read(n, 0, size) == dev.files[n][:size]


def test_mismatched_magic_cmd_seq_raise():
    def flip(i, new):
        return lambda r: r[:i] + new + r[i + len(new):]

    for name, tamper in (("magic", flip(0, b"XGP1")),
                         ("cmd", flip(4, struct.pack("<H", 0x0002 | 0x8000))),
                         ("request cmd without 0x8000", flip(4, struct.pack("<H", 0x0001))),
                         ("seq", flip(8, struct.pack("<I", 99)))):
        dev, c = mk()
        dev.tamper = tamper
        with pytest.raises(uc.ProtocolError):
            c.status()


def test_truncated_and_oversized_response():
    dev, c = mk()
    dev.tamper = lambda r: r[:-3]
    with pytest.raises(TimeoutError):
        c.status()
    dev, c = mk()
    dev.tamper = lambda r: r + b"xx"
    with pytest.raises(uc.ProtocolError):
        c.status()


def test_negative_status_raises_with_errno_name():
    dev, c = mk()
    with pytest.raises(uc.DeviceError) as ei:
        c.hash("20261031-999999.jpg")
    assert ei.value.status == -ENOENT and ei.value.errno_name == "ENOENT"
    assert "ENOENT" in str(ei.value)
    with pytest.raises(uc.DeviceError) as ei:
        c.hash("../etc/passwd")
    assert ei.value.errno_name == "EINVAL" and ei.value.status == -22


def test_oversized_request_is_refused_locally():
    dev, c = mk()
    with pytest.raises(ValueError):
        c.request(uc.CMD_STATUS, b"x" * 65537)
    assert dev.requests == []


# -- list ------------------------------------------------------------------------------------------------------


def test_list_after_and_max():
    dev, c = mk(make_files(5))
    names = sorted(dev.files)
    r = c.list("", 2)
    assert [f["name"] for f in r["files"]] == names[:2] and r["more"] is True
    r = c.list(names[1], 10)
    assert [f["name"] for f in r["files"]] == names[2:] and r["more"] is False
    assert json.loads(dev.requests[0][2]) == {"after": "", "max": 2}


def test_list_all_across_several_pages():
    files = {"20261031-%06d.snap" % i: b"x" for i in range(23)}
    dev, c = mk(files)
    got = c.list_all(page=5)
    assert [f["name"] for f in got] == sorted(files)
    assert len(dev.requests) == 5  # 5+5+5+5+3
    got = c.list_all()  # default page of 500
    assert len(got) == 23


def test_list_all_empty():
    dev, c = mk({})
    assert c.list_all() == []


# -- download --------------------------------------------------------------------------------------------------


def test_download_writes_correct_files(tmp_path):
    files = make_files(3, size=70000)  # more than one 64 KiB chunk
    dev, c = mk(files)
    res = c.download(sorted(files), tmp_path / "out")
    assert all(r["ok"] for r in res)
    for r in res:
        data = files[r["name"]]
        assert (tmp_path / "out" / r["name"]).read_bytes() == data
        assert r["size"] == len(data) and r["sha256"] == hashlib.sha256(data).hexdigest()
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(files)  # no .part left
    reads = [json.loads(p) for cmd, _, p in dev.requests if cmd == uc.CMD_READ]
    assert max(r["len"] for r in reads) == 65532 and reads[0]["offset"] == 0


def test_download_rejects_corrupted_chunk(tmp_path):
    files = make_files(2, size=5000)
    a, b = sorted(files)
    dev, c = mk(files)
    dev.corrupt_once.add((a, 0))
    res = c.download([a, b], tmp_path)
    assert not res[0]["ok"] and "mismatch" in res[0]["error"]
    assert res[0]["sha256"] != res[0]["device_sha256"] == hashlib.sha256(files[a]).hexdigest()
    assert res[1]["ok"]
    assert not (tmp_path / a).exists() and not (tmp_path / (a + ".part")).exists()
    assert (tmp_path / b).read_bytes() == files[b]
    # a retry (the fake corrupts only once) succeeds
    assert c.download([a], tmp_path)[0]["ok"]


def test_download_does_not_clobber_existing_file_on_failure(tmp_path):
    files = make_files(1)
    a = sorted(files)[0]
    (tmp_path / a).write_bytes(b"old")
    dev, c = mk(files)
    dev.corrupt_once.add((a, 0))
    assert not c.download([a], tmp_path)[0]["ok"]
    assert (tmp_path / a).read_bytes() == b"old"


def test_download_reports_device_error_and_invalid_name(tmp_path):
    dev, c = mk(make_files(1))
    res = c.download(["20261031-999999.jpg", "../evil.jpg"], tmp_path)
    assert not res[0]["ok"] and "ENOENT" in res[0]["error"]
    assert not res[1]["ok"] and "invalid" in res[1]["error"]
    assert list(tmp_path.iterdir()) == []
    assert len(dev.requests) == 1  # the invalid name never reached the device


# -- delete ----------------------------------------------------------------------------------------------------


def test_delete_with_correct_hash():
    dev, c = mk(make_files(3))
    a, b, d = sorted(dev.files)
    h = c.hash(a)["sha256"]
    r = c.delete([{"name": a, "sha256": h}])
    assert r["results"] == [{"name": a, "ok": True}]
    assert a not in dev.files and b in dev.files


def test_delete_refuses_when_hash_is_wrong():
    dev, c = mk(make_files(2))
    a, b = sorted(dev.files)
    r = c.delete([{"name": a, "sha256": "0" * 64}, {"name": b, "sha256": c.hash(b)["sha256"]}])
    assert [x["ok"] for x in r["results"]] == [False, True]
    assert a in dev.files and b not in dev.files


def test_delete_verified_hashes_first_and_reports_missing():
    dev, c = mk(make_files(3))
    a, b, d = sorted(dev.files)
    res = c.delete_verified([a, "20261031-999999.jpg", d])
    assert [(r["name"], r["ok"]) for r in res] == [(a, True), ("20261031-999999.jpg", False), (d, True)]
    assert set(dev.files) == {b}
    cmds = [cmd for cmd, _, _ in dev.requests]
    assert cmds == [uc.CMD_HASH] * 3 + [uc.CMD_DELETE]


def test_delete_verified_batches_of_500():
    files = {"20261031-%06d.snap" % i: b"x" for i in range(501)}
    dev, c = mk(files)
    res = c.delete_verified(sorted(files))
    assert len(res) == 501 and all(r["ok"] for r in res) and dev.files == {}
    sizes = [len(json.loads(p)["files"]) for cmd, _, p in dev.requests if cmd == uc.CMD_DELETE]
    assert sizes == [500, 1]


# -- clock -----------------------------------------------------------------------------------------------------


def test_time_offset_estimate_symmetric():
    dev, c = mk(clip_offset_ms=2500)
    dev.time_delays = [(10, 10), (4, 4), (0.5, 0.5), (7, 7)]
    m = c.measure_offset(rounds=4)
    assert m["rounds"] == 4 and abs(m["best_rtt_ms"] - 1.0) < 1e-6
    assert abs(m["offset_ms"] - 2500) < 1.0


def test_sync_clock_uses_smallest_rtt_sample():
    dev, c = mk(clip_offset_ms=-3217)
    # slow, asymmetric samples would mislead the estimate; the 1 ms round trip (0.5 ms each way) is the best
    dev.time_delays = [(30, 5), (2, 9), (0.5, 0.5), (12, 3), (4, 4), (6, 20), (1.5, 1.5), (8, 8)]
    dev.other_delay = (0.5, 0.5)  # the SETTIME request takes as long as the best TIME request one way
    r = c.sync_clock(rounds=8)
    assert r["rounds"] == 8
    assert abs(r["best_rtt_ms"] - 1.0) < 1e-6
    assert abs(r["offset_before_ms"] - (-3217)) < 1.0
    # utc_ms must equal the host's wall clock at the moment the device processed SETTIME
    assert abs(dev.settime_utc_ms - dev.settime_host_ms) < 1.0
    assert r["sent_utc_ms"] == dev.settime_utc_ms
    assert r["reply"]["rtc"] == "ok" and r["reply"]["before_ms"] != r["reply"]["after_ms"]
    # afterwards the Clip's clock agrees with the host's
    assert abs(dev.clip_offset_ms) <= 1
    cmds = [cmd for cmd, _, _ in dev.requests]
    assert cmds == [uc.CMD_TIME] * 8 + [uc.CMD_SETTIME]


def test_sync_clock_default_is_eight_rounds():
    dev, c = mk()
    c.sync_clock()
    assert sum(cmd == uc.CMD_TIME for cmd, _, _ in dev.requests) == 8


# -- bench -----------------------------------------------------------------------------------------------------


def test_bench_reads_whole_file():
    n = "20261031-101500.jpg"
    dev, c = mk({n: bytes(200000)})
    dev.other_delay = (1, 1)
    r = c.bench(n)
    assert r["bytes"] == 200000 and r["seconds"] > 0 and r["mib_per_s"] > 0
    r = c.bench(n, max_bytes=70000)
    assert r["bytes"] == 70000


# -- CLI -------------------------------------------------------------------------------------------------------


def test_cli_delete_without_yes_refuses(capsys):
    dev, _ = mk()
    names = sorted(dev.files)
    assert uc.main(["delete", names[0]], transport=dev) == 2
    assert "--yes" in capsys.readouterr().err
    assert dev.requests == [] and set(dev.files) == set(names)


def test_cli_delete_with_yes(capsys):
    dev, _ = mk()
    names = sorted(dev.files)
    assert uc.main(["delete", names[0], "--yes"], transport=dev) == 0
    assert names[0] not in dev.files and "deleted" in capsys.readouterr().out
    assert [c for c, _, _ in dev.requests] == [uc.CMD_HASH, uc.CMD_DELETE]


def test_cli_delete_missing_file_fails(capsys):
    dev, _ = mk()
    assert uc.main(["delete", "20261031-999999.jpg", "--yes"], transport=dev) == 1
    assert "NOT DELETED" in capsys.readouterr().out


def test_cli_get_all_and_json(tmp_path, capsys):
    dev, _ = mk(make_files(3))
    assert uc.main(["--json", "get", "--all", "--dest", str(tmp_path)], transport=dev) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out] == sorted(dev.files) and all(r["ok"] for r in out)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(dev.files)


def test_cli_get_needs_names_or_all(tmp_path, capsys):
    dev, _ = mk()
    assert uc.main(["get", "--dest", str(tmp_path)], transport=dev) == 2
    assert uc.main(["get", "--all", "20261031-101500.jpg", "--dest", str(tmp_path)], transport=dev) == 2


def test_cli_status_list_hash_time(capsys):
    dev, _ = mk()
    assert uc.main(["status"], transport=dev) == 0
    assert "version: fake" in capsys.readouterr().out
    assert uc.main(["list", "--max", "2"], transport=dev) == 0
    out = capsys.readouterr().out
    assert "more available" in out
    assert uc.main(["hash", sorted(dev.files)[0], "--json"], transport=dev) == 0
    assert len(json.loads(capsys.readouterr().out)["sha256"]) == 64
    assert uc.main(["time"], transport=dev) == 0
    assert "Clip minus host" in capsys.readouterr().out
    assert not any(cmd == uc.CMD_SETTIME for cmd, _, _ in dev.requests)


def test_cli_device_error_is_reported(capsys):
    dev, _ = mk()
    assert uc.main(["hash", "20261031-999999.jpg"], transport=dev) == 1
    assert "ENOENT" in capsys.readouterr().err


def test_cli_settime(capsys):
    dev, _ = mk(clip_offset_ms=1000)
    assert uc.main(["settime", "--rounds", "3"], transport=dev) == 0
    assert "offset before" in capsys.readouterr().out
    assert dev.settime_utc_ms is not None


def test_help_works_without_pyusb():
    with pytest.raises(SystemExit) as ei:
        uc.main(["--help"])
    assert ei.value.code == 0
