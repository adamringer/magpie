#!/usr/bin/env python3
"""Diagnose corrupted downloads the way `usbclient.py get` does them (READ to EOF, then HASH), over many files,
against reference copies in REFDIR (fetched by FTP). Bad copies are kept as OUTDIR/<name>.bad and the first
difference is described.

    /usr/bin/python3 -I tools/phone/getdiag.py REFDIR OUTDIR [ROUNDS]
"""
import hashlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usbclient as uc  # noqa: E402

refdir, outdir = sys.argv[1], sys.argv[2]
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3
os.makedirs(outdir, exist_ok=True)
names = sorted(n for n in os.listdir(refdir) if not n.startswith('.'))
c = uc.Client(uc.UsbTransport())
bad = 0
for r in range(rounds):
    for name in names:
        ref = open(os.path.join(refdir, name), 'rb').read()
        buf = bytearray(); off = 0
        while True:
            got = c.read(name, off, 65532)
            if not got:
                break
            buf += got; off += len(got)
        h = c.hash(name)
        if bytes(buf) != ref:
            bad += 1
            n = min(len(buf), len(ref))
            i = next((k for k in range(n) if buf[k] != ref[k]), n)
            j = i
            while j < n and buf[j] != ref[j]:
                j += 1
            seg = bytes(buf[i:i + 16]); where = ref.find(seg) if len(seg) == 16 else -1
            print('round %d %s: len got %d want %d, first diff at %d (chunk %d, +%d), run %d bytes, got data found in '
                  'ref at %s (shift %s); device hash ok=%s' % (
                      r, name, len(buf), len(ref), i, i // 65532, i % 65532, j - i, where,
                      where - i if where >= 0 else '-', h['sha256'] == hashlib.sha256(ref).hexdigest()))
            print('   got  %s\n   want %s' % (bytes(buf[i:i + 32]).hex(), ref[i:i + 32].hex()))
            open(os.path.join(outdir, name + '.bad.r%d' % r), 'wb').write(buf)
print('rounds %d x %d files, bad %d' % (rounds, len(names), bad))
c.t.close()
