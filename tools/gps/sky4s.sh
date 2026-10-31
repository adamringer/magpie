#!/bin/sh
# sky4s.sh [COUNT] [PERIOD_S] [DELAY_S]: standalone long GPS captures for an open-sky test (no camera, no fast
# resume). Waits DELAY_S (default 60, time to carry the Clip outside), then takes COUNT (default 10) 4096 ms
# captures with the LNA on, one every PERIOD_S (default 30) seconds, into /data/gps/sky4s/.
# One line per capture in /data/gps/sky4s/log.txt (UTC time, file, gpsnap exit code), synced each time.
# LED d1 lights during each capture and all four flash twice when the run is done.
# magpie-lifelogd and magpie-chargeledd are stopped for the run and restarted at the end: their per-second
# busybox execs stall gpsnap long enough (>= ~32 ms) for the GPS chip to stop mid-capture (DATA READY
# timeout, STATUS_MAGPIE 62c). A capture that still times out is retried once.
# Only one instance runs (lock dir). Start it detached; it survives the telnet session and USB unplug:
#   start-stop-daemon -S -b -x /data/sky4s.sh -- 10 30 120
COUNT=${1:-10}; PERIOD=${2:-30}; DELAY=${3:-60}
OUT=/data/gps/sky4s; mkdir -p "$OUT"; LOG=$OUT/log.txt
LOCK=/run/sky4s.lock
mkdir "$LOCK" 2>/dev/null || { echo "$(date -u '+%F %T') already running, not started" >> "$LOG"; exit 1; }
led() { for l in /sys/class/leds/$1/brightness; do [ -w "$l" ] && echo "$2" > "$l"; done; }
restore() {
	/etc/init.d/S50lifelog start; /etc/init.d/S51chargeled start
	rmdir "$LOCK"
}
trap 'restore; exit 1' INT TERM
echo "$(date -u '+%F %T') start count=$COUNT period=$PERIOD delay=$DELAY" >> "$LOG"; sync
/etc/init.d/S51chargeled stop; /etc/init.d/S50lifelog stop
led 'magpie:d*' 0
sleep "$DELAY"
i=1
while [ "$i" -le "$COUNT" ]; do
	t0=$(date +%s)
	for try in 1 2; do
		f=$OUT/$(date -u +%Y%m%d-%H%M%S).snap
		led 'magpie:d1' 255; gpsnap -s 4096 -a 1 -v -o "$f" >> "$OUT/gpsnap.out" 2>&1; rc=$?; led 'magpie:d1' 0
		echo "$(date -u '+%F %T') $i $f rc=$rc try=$try" >> "$LOG"; sync
		[ "$rc" = 3 ] || break
	done
	i=$((i + 1))
	if [ "$i" -le "$COUNT" ]; then
		w=$((PERIOD - ($(date +%s) - t0))); [ "$w" -gt 0 ] && sleep "$w"
	fi
done
for n in 1 2; do led 'magpie:d*' 255; sleep 1; led 'magpie:d*' 0; sleep 1; done
echo "$(date -u '+%F %T') done" >> "$LOG"; sync
restore
