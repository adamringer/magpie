import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import samba  # noqa: E402
from samba import Samba, SambaError  # noqa: E402


class FakeMonitor:
    def __init__(self):
        self.mem = {}
        self.timeout = 1
        self.rx = bytearray()
        self.out = bytearray()
        self.log = []
        self.writes = []
        self.pending = None
        self.version_reply = b"v1.0 Jan 1 2012\r\n\x00\xff"

    def reset_input_buffer(self):
        self.rx.clear()

    def flush(self):
        pass

    def write(self, data):
        self.rx += data
        self._parse()

    def _parse(self):
        while True:
            if self.pending:
                addr, left = self.pending
                if not self.rx:
                    return
                take = bytes(self.rx[:left])
                del self.rx[:len(take)]
                for i, b in enumerate(take):
                    self.mem[addr + i] = b
                left -= len(take)
                self.pending = (addr + len(take), left) if left else None
                continue
            i = self.rx.find(b"#")
            if i < 0:
                return
            cmd = bytes(self.rx[:i]).decode()
            del self.rx[:i + 1]
            self.log.append(cmd + "#")
            self._exec(cmd)

    def _exec(self, cmd):
        c, arg = cmd[:1], cmd[1:]
        if c == "N":
            self.out += b"\n\r"
        elif c == "V":
            self.out += self.version_reply
        elif c == "w":
            a = int(arg.split(",")[0], 16)
            self.out += bytes(self.mem.get(a + i, 0) for i in range(4))
        elif c == "W":
            a, v = (int(x, 16) for x in arg.split(","))
            self.writes.append((a, v))
            for i in range(4):
                self.mem[a + i] = (v >> (8 * i)) & 0xFF
        elif c == "o":
            a = int(arg.split(",")[0], 16)
            self.out.append(self.mem.get(a, 0))
        elif c == "O":
            a, v = (int(x, 16) for x in arg.split(","))
            self.mem[a] = v
        elif c == "R":
            a, n = (int(x, 16) for x in arg.split(","))
            self.out += bytes(self.mem.get(a + i, 0) for i in range(n))
        elif c == "S":
            a, n = (int(x, 16) for x in arg.split(","))
            self.pending = (a, n)
        elif c == "G":
            pass

    def read(self, n=1):
        data = bytes(self.out[:n])
        del self.out[:n]
        return data


@pytest.fixture
def fake():
    return FakeMonitor()


@pytest.fixture
def sb(fake):
    return Samba(fake)


def test_init_sends_n(fake, sb):
    fake.out += b"junk"
    sb.init()
    assert fake.log == ["N#"]
    assert fake.out == b""
    assert fake.timeout == 1


def test_version_strips_nonprintable(fake, sb):
    assert sb.version() == "v1.0 Jan 1 2012"
    assert fake.log == ["V#"]


def test_write_read32_roundtrip_and_bytes(fake, sb):
    sb.write32(0x20000000, 0xDEADBEEF)
    assert sb.read32(0x20000000) == 0xDEADBEEF
    assert fake.log == ["W20000000,DEADBEEF#", "w20000000,4#"]
    sb.write32(0x10, 0xA)
    sb.read32(0x10)
    assert fake.log[2:] == ["W00000010,0000000A#", "w00000010,4#"]


def test_read32_little_endian(fake, sb):
    fake.mem.update({0x100: 0x78, 0x101: 0x56, 0x102: 0x34, 0x103: 0x12})
    assert sb.read32(0x100) == 0x12345678


def test_short_read_raises(fake, sb):
    fake.read = lambda n=1: b"\x01\x02"
    with pytest.raises(SambaError):
        sb.read32(0)


def test_byte_ops(fake, sb):
    sb.write8(0x30, 0xAB)
    assert sb.read8(0x30) == 0xAB
    assert fake.log == ["O00000030,AB#", "o00000030,4#"]


def test_bulk_roundtrip_chunks(fake, sb):
    data = bytes(i * 7 % 256 for i in range(10000))
    sb.write(0x20000000, data)
    assert [c for c in fake.log if c[0] == "S"] == [
        "S20000000,00001000#", "S20001000,00001000#", "S20002000,00000710#"]
    assert sb.read(0x20000000, 10000) == data
    assert len([c for c in fake.log if c[0] == "R"]) == 3


def test_read_avoids_rom_512_tail(fake, sb):
    # ROM v1.4 loses a final full 512-byte packet (length % 2048 == 512)
    data = bytes(i * 3 % 256 for i in range(4096 * 75 + 2560))
    sb.write(0x21100000, data)
    assert sb.read(0x21100000, len(data)) == data
    lengths = [int(c.split(",")[1].rstrip("#"), 16) for c in fake.log if c[0] == "R"]
    assert sum(lengths) == len(data)
    assert all(n % 2048 != 512 for n in lengths)
    assert sb.read(0x21100000, 512) == data[:512]


def test_go(fake, sb):
    sb.go(0x20000000)
    assert fake.log == ["G20000000#"]


def test_set_bootmode_ok(fake):
    assert samba.main(["set-bootmode", "5"], ser=fake) == 0
    assert (0xFFFFFE54, 0x66830005) in fake.writes


def test_set_bootmode_mismatch(fake):
    # a real BSC_CR ignores the key and keeps only BOOT; make the fake ignore the write
    real_exec = fake._exec

    def stuck(cmd):
        if cmd[0] == "W":
            fake.writes.append(tuple(int(x, 16) for x in cmd[1:].split(",")))
            return
        real_exec(cmd)

    fake._exec = stuck
    fake.mem[0xFFFFFE54] = 4
    assert samba.main(["set-bootmode", "5"], ser=fake) == 1
    assert fake.writes == [(0xFFFFFE54, 0x66830005)]


def test_reset(fake):
    assert samba.main(["reset"], ser=fake) == 0
    assert (0xFFFFFE00, 0xA5000005) in fake.writes


def test_find_port(monkeypatch):
    ports = [SimpleNamespace(vid=0x1234, pid=0x6124, device="/dev/ttyUSB0"),
             SimpleNamespace(vid=0x03EB, pid=0x6124, device="/dev/ttyACM3"),
             SimpleNamespace(vid=None, pid=None, device="/dev/ttyS0")]
    monkeypatch.setattr(samba.serial.tools.list_ports, "comports", lambda: ports)
    assert samba.find_port() == "/dev/ttyACM3"


def test_find_port_missing(monkeypatch):
    ports = [SimpleNamespace(vid=0x1234, pid=0x5678, device="/dev/ttyUSB0")]
    monkeypatch.setattr(samba.serial.tools.list_ports, "comports", lambda: ports)
    with pytest.raises(SambaError):
        samba.find_port()


def test_number_parsing():
    assert samba.num("0xFFFFFE54") == 0xFFFFFE54
    assert samba.num("4096") == 4096
    args = samba.build_parser().parse_args(["write32", "0x10", "255"])
    assert (args.addr, args.value) == (16, 255)


def test_main_read32_output(fake, capsys):
    fake.mem[0x20] = 0x01
    assert samba.main(["read32", "32"], ser=fake) == 0
    assert capsys.readouterr().out.strip() == "0x00000020: 0x00000001"


def test_main_dump_load(fake, tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"hello world")
    assert samba.main(["load", "0x1000", str(src)], ser=fake) == 0
    out = tmp_path / "out.bin"
    assert samba.main(["dump", "0x1000", "11", str(out)], ser=fake) == 0
    assert out.read_bytes() == b"hello world"
