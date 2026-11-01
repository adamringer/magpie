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
# optional 4th argument: gpsnap -w warm-up values (ms) to cycle through, e.g. "0,1000" for an A/B test
WARMUPS=$(echo "${4:-0}" | tr ',' ' ')
# optional 5th argument: LED d1 during each capture, 1/0 values to cycle through (default 1). 0 keeps all LEDs
# off while the chip samples (the stock Clip's GPS was sensitive to the camera's own electronics)
LEDS=$(echo "${5:-1}" | tr ',' ' ')
# optional 6th argument: after capture N, flash all LEDs for 5 s and pause 60 s (cue to change the orientation).
# Not needed for orientation tests any more: every log line carries an accelerometer reading (acc=x,y,z in mg,
# LIS3DH axes, sampled just after the capture so the i2c execs can't stall gpsnap), so the orientation of each
# capture is known whenever the Clip was turned.
CUE_AT=${6:-0}
ACCEL=magpie-accel; command -v "$ACCEL" > /dev/null || ACCEL=""
# a newer gpsnap test build in /data wins (writes the PATH.time capture timestamp)
GPSNAP=gpsnap; [ -x /data/gpsnap.new ] && GPSNAP=/data/gpsnap.new
OUT=/data/gps/sky4s; mkdir -p "$OUT"; LOG=$OUT/log.txt
LOCK=/run/sky4s.lock
mkdir "$LOCK" 2>/dev/null || { echo "$(date -u '+%F %T') already running, not started" >> "$LOG"; exit 1; }
led() { for l in /sys/class/leds/$1/brightness; do [ -w "$l" ] && echo "$2" > "$l"; done; }
restore() {
	/etc/init.d/S50lifelog start; /etc/init.d/S51chargeled start
	rmdir "$LOCK"
}
trap 'restore; exit 1' INT TERM
echo "$(date -u '+%F %T') start count=$COUNT period=$PERIOD delay=$DELAY gpsnap=$GPSNAP" >> "$LOG"; sync
/etc/init.d/S51chargeled stop; /etc/init.d/S50lifelog stop
led 'magpie:d*' 0
[ -n "$ACCEL" ] && { $ACCEL prep || ACCEL=""; }   # takes the chip from st_accel_i2c; lifelog re-applies its mode at restart
sleep "$DELAY"
i=1
while [ "$i" -le "$COUNT" ]; do
	t0=$(date +%s)
	set -- $WARMUPS; n=$#; k=$(( (i - 1) % n + 1 )); eval "w=\${$k}"
	wopt=""; [ "$w" != 0 ] && wopt="-w $w"   # an older gpsnap without -w still works for 0
	set -- $LEDS; n=$#; k=$(( (i - 1) % n + 1 )); eval "ledon=\${$k}"
	for try in 1 2; do
		f=$OUT/$(date -u +%Y%m%d-%H%M%S).snap
		[ "$ledon" = 1 ] && led 'magpie:d1' 255
		$GPSNAP -s 4096 -a 1 $wopt -v -o "$f" >> "$OUT/gpsnap.out" 2>&1; rc=$?; led 'magpie:d1' 0
		acc=-; [ -n "$ACCEL" ] && { set -- $($ACCEL sample 2> /dev/null); [ $# -eq 6 ] && acc=$4,$5,$6; }
		echo "$(date -u '+%F %T') $i $f rc=$rc try=$try warmup=$w led=$ledon acc=$acc" >> "$LOG"; sync
		[ "$rc" = 3 ] || break
	done
	if [ "$CUE_AT" -gt 0 ] && [ "$i" -eq "$CUE_AT" ]; then
		echo "$(date -u '+%F %T') cue: change orientation (60 s)" >> "$LOG"; sync
		for n in 1 2 3 4 5; do led 'magpie:d*' 255; sleep 0.5; led 'magpie:d*' 0; sleep 0.5; done
		sleep 55; t0=$(date +%s)
	fi
	i=$((i + 1))
	if [ "$i" -le "$COUNT" ]; then
		w=$((PERIOD - ($(date +%s) - t0))); [ "$w" -gt 0 ] && sleep "$w"
	fi
done
for n in 1 2; do led 'magpie:d*' 255; sleep 1; led 'magpie:d*' 0; sleep 1; done
echo "$(date -u '+%F %T') done" >> "$LOG"; sync
restore
