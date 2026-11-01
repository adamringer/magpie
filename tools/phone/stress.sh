#!/bin/sh
# stress.sh ROUNDS REFDIR OUTDIR: getdiag.py downloads while FTP uploads flood the Clip's network link (ECM OUT
# traffic), to check that phone-interface downloads stay intact. Run on the laptop from the repo root.
set -u
cd "$(dirname "$0")/../.." || exit 1
R=${1:-6}; REF=${2:-/tmp/ref30}; OUT=${3:-/tmp/bad_stress}
head -c 2000000 /dev/urandom > /tmp/stress_junk.bin
( while :; do curl -s -T /tmp/stress_junk.bin ftp://192.168.2.2/tmp/stress_junk.bin; done ) &
LOOP=$!
trap 'kill $LOOP 2>/dev/null; wait $LOOP 2>/dev/null; pkill -x -P $LOOP curl 2>/dev/null; for p in $(pgrep -x curl); do tr "\\0" " " < /proc/$p/cmdline | grep -q stress_junk && kill $p; done; curl -s -Q "DELE /tmp/stress_junk.bin" ftp://192.168.2.2/ > /dev/null 2>&1' EXIT
rm -rf "$OUT"
S=$(date +%s)
/usr/bin/python3 -I tools/phone/getdiag.py "$REF" "$OUT" "$R"
echo "took $(( $(date +%s) - S )) s, bad copies kept: $(ls "$OUT" 2> /dev/null | wc -l)"
