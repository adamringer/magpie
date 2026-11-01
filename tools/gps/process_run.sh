#!/bin/sh
# process_run.sh DIR SEED [REF] [MS]
# Position fixes for every .snap capture in DIR:
#   1. broadcast ephemeris for the captures' day (nav.py fetch: NOAA first) into DIR, unless already there
#   2. blind search (snapfix.py --lengths MS) -> DIR/meas.jsonl (captures already measured are skipped)
#   3. blind run-level solve (joint.py) -> clock offset T
#   4. aided search (aided.py --null, capture times + T) -> DIR/aided_meas.jsonl, threshold from the null test
#   5. run-level solves, blind and aided -> DIR/joint_{blind,aided}_MS.log and .kml
# SEED: file with "lat lon" (last known position, within a few hundred metres). REF (optional): "lat lon" of
# the true position (e.g. a survey mark) -> distances to it in the logs. MS: capture length to use (default 512,
# the lifelog length; longer captures are cut). Outputs contain locations: keep DIR under analysis/.
# Uses the system python3 (numpy, scipy). Captures spanning two UTC days use the first day's ephemeris.
set -eu
[ $# -ge 2 ] || { sed -n '2,12p' "$0"; exit 1; }
DIR=$1; SEED=$2; REF=${3:-}; MS=${4:-512}
T=$(cd "$(dirname "$0")" && pwd)
PY="python3 -I"
first=$(ls "$DIR"/*.snap | head -1)
[ -n "$first" ] || { echo "no .snap in $DIR"; exit 1; }
day=$(basename "$first" | cut -c1-8)
year=$(echo "$day" | cut -c1-4)
doy=$(date -u -d "$day" +%j)
yy=$(echo "$year" | cut -c3-4)
NAV=$DIR/brdc${doy}0.${yy}n.gz
[ -s "$NAV" ] || $PY "$T/nav.py" fetch "$year" "$(expr "$doy" + 0)" "$DIR"
REFARG=""; [ -n "$REF" ] && REFARG="--ref $REF"
# every capture needs its gpsnap .time sidecar (capture time to ~20 ms; file-name times are not supported)
for f in "$DIR"/*.snap; do
	[ -s "$f.time" ] || { echo "missing $f.time: captures without a .time sidecar are not supported"; exit 1; }
done

echo "== blind search ($MS ms)"
for f in "$DIR"/*.snap; do
	n=$(basename "$f")
	grep -q "\"name\": \"$n\", \"ms\": $MS," "$DIR/meas.jsonl" 2> /dev/null && continue
	$PY "$T/snapfix.py" "$f" "$NAV" --zmin 8 --lengths "$MS" --meas-out "$DIR/meas.jsonl" > "$DIR/meas_${n%.snap}.log" 2>&1 ||
		echo "  $n: snapfix failed, see $DIR/meas_${n%.snap}.log"
done

echo "== blind solve"
$PY "$T/joint.py" "$DIR/meas.jsonl" "$NAV" --ms "$MS" --zmin 8 --sigma 0.02 --seed-file "$SEED" $REFARG \
	--kml "$DIR/fixes_blind_$MS.kml" > "$DIR/joint_blind_$MS.log" 2>&1 || true
head -1 "$DIR/joint_blind_$MS.log"
TC=$(sed -n 's/.*clock offset T = \([-+0-9.]*\) s.*/\1/p' "$DIR/joint_blind_$MS.log" | head -1)
[ -n "$TC" ] || { TC=0; echo "  no clock offset from the blind solve, using T = 0"; }

echo "== aided search (T = $TC s)"
rm -f "$DIR/aided_meas.jsonl"
$PY "$T/aided.py" "$DIR/meas.jsonl" "$NAV" --dir "$DIR" --seed-file "$SEED" --ms "$MS" --tc "$TC" --null \
	--meas-out "$DIR/aided_meas.jsonl" > "$DIR/aided_$MS.log" 2>&1
tail -2 "$DIR/aided_$MS.log"

echo "== aided solve"
$PY "$T/joint.py" "$DIR/aided_meas.jsonl" "$NAV" --ms "$MS" --zmin 0 --sigma 0.02 --seed-file "$SEED" $REFARG \
	--kml "$DIR/fixes_aided_$MS.kml" > "$DIR/joint_aided_$MS.log" 2>&1 || true
head -1 "$DIR/joint_aided_$MS.log"

$PY - "$DIR" "$MS" << 'EOF'
import re, statistics as st, sys
d, ms = sys.argv[1], sys.argv[2]
for src in ('blind', 'aided'):
    try:
        t = open('%s/joint_%s_%s.log' % (d, src, ms)).read()
    except OSError:
        continue
    unit = 'to ref' if 'to ref' in t else 'to median'
    for col in ('alone', 'with T'):
        v = [int(x) for x in re.findall(r'%s:\s+(\d+) m' % col, t)]
        n = len(re.findall(r'\d+ PRN \|', t))
        if v:
            print('%-5s %-6s: %2d/%d fixes, median %4d m, max %4d m %s' % (src, col, len(v), n, st.median(v), max(v), unit))
EOF
