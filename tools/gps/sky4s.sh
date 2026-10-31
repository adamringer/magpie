#!/bin/sh
# sky4s.sh [COUNT] [PERIOD_S] [DELAY_S]: standalone long GPS captures for an open-sky test (lifelog stopped,
# no camera, no fast resume). Waits DELAY_S (default 60, time to carry the Clip outside), then takes COUNT
# (default 10) 4096 ms captures with the LNA on, one every PERIOD_S (default 30) seconds, into /data/gps/sky4s/.
# One line per capture in /data/gps/sky4s/log.txt (UTC time, file, gpsnap exit code), synced each time.
# LED d1 lights during each capture and all four flash twice when the run is done.
# Run detached so unplugging USB doesn't kill it:  setsid /data/sky4s.sh 10 30 60 </dev/null >/dev/null 2>&1 &
COUNT=${1:-10}; PERIOD=${2:-30}; DELAY=${3:-60}
OUT=/data/gps/sky4s; mkdir -p "$OUT"; LOG=$OUT/log.txt
led() { for l in /sys/class/leds/$1/brightness; do [ -w "$l" ] && echo "$2" > "$l"; done; }
echo "$(date -u '+%F %T') start count=$COUNT period=$PERIOD delay=$DELAY" >> "$LOG"; sync
sleep "$DELAY"
i=1
while [ "$i" -le "$COUNT" ]; do
	t0=$(date +%s); f=$OUT/$(date -u +%Y%m%d-%H%M%S).snap
	led 'magpie:d1' 255; gpsnap -s 4096 -a 1 -v -o "$f" >> "$OUT/gpsnap.out" 2>&1; rc=$?; led 'magpie:d1' 0
	echo "$(date -u '+%F %T') $i $f rc=$rc" >> "$LOG"; sync
	i=$((i + 1)); [ "$i" -le "$COUNT" ] && sleep $((PERIOD - ($(date +%s) - t0)))
done
for n in 1 2; do led 'magpie:d*' 255; sleep 1; led 'magpie:d*' 0; sleep 1; done
echo "$(date -u '+%F %T') done" >> "$LOG"; sync
