# Sourced by magpie-lifelogd and magpie-accel: defaults, config file loading and logging for the lifelog daemon.
# Every variable can be overridden from the environment first (the host tests do this to stub paths and
# commands), then from /data/lifelog.conf (see /etc/lifelog.conf.example). POSIX sh, BusyBox ash.

# ---- tunables (documented in /etc/lifelog.conf.example and docs/LIFELOG.md) ----
: "${INTERVAL:=30}"              # target wake-to-wake period in seconds
: "${GPS:=1}"                    # 1 = take a GPS snapshot with every photo
: "${BLINK:=1}"                  # 1 = blink one LED per photo
: "${BLINK_LED:=1}"              # which LED (1-4)
: "${BLINK_LEVEL:=1}"            # 1 = the photo blink shows the battery level on LEDs 1..N
: "${OFF_LEVEL:=205}"            # magpie-battery raw value: below this, power off for good (stock scale)
: "${WARN_LEVEL:=207}"           # at or below this, log a warning (stock scale)
: "${MIN_ALARM:=8}"              # shortest RTC alarm in seconds
: "${BOOT_PREAMBLE:=5}"          # seconds from alarm to kernel start (bootloaders), subtracted from the alarm
: "${VBUS_CHECKS:=3}"            # consecutive 1 s checks without VBUS before a cycle starts
: "${DARK_JPEG_BYTES:=60000}"    # photo smaller than this counts as dark (to be calibrated)
: "${DARK_AFTER:=2}"             # dark streak above this -> DARK_INTERVAL
: "${DARK_INTERVAL:=120}"
: "${DARK_LONG_AFTER:=10}"       # dark streak above this -> DARK_LONG_INTERVAL
: "${DARK_LONG_INTERVAL:=480}"
: "${FACEDOWN_AXIS:=z}"          # x, y or z; empty = face-down detection disabled. Measured 2026-10-30: camera down = Z -1008 mg
: "${FACEDOWN_SIGN:=-1}"         # 1 or -1: face-down when SIGN * axis_mg >= FACEDOWN_THRESHOLD
: "${FACEDOWN_THRESHOLD:=800}"   # milli-g
: "${FACEDOWN_OTHER_MAX:=350}"   # the other two axes must each be below this (abs, milli-g), as stock
: "${FACEDOWN_INTERVAL:=120}"
: "${FACEDOWN_LONG_AFTER:=10}"   # face-down streak above this -> FACEDOWN_LONG_INTERVAL
: "${FACEDOWN_LONG_INTERVAL:=240}"
: "${LOG_MAX:=1048576}"          # rotate log.csv and daemon.log at this size (bytes)
: "${WAIT_TIMEOUT:=15}"          # seconds to wait for /dev/video0 and the spidev node
: "${DATA_WAIT:=20}"             # seconds to wait for /data to be mounted
: "${SNAP_TIMEOUT:=30}"
: "${GPSNAP_TIMEOUT:=30}"
: "${GPS_ARGS:=-a 1}"            # arguments for gpsnap (-o PATH is added)
: "${NOALARM_RETRY:=30}"         # seconds to stay awake before retrying when the RTC alarm cannot be set
: "${SLEEP_MODE:=resume}"        # resume = sleep with fast resume between cycles; poweroff = power off after every cycle
: "${COLD_EVERY:=70}"            # resume mode: every Nth sleep is a real power-off + RTC wake (cold boot); 0 = never
: "${RESUME_MAX_FAIL:=3}"        # failed fast resumes (cold boot with GPBR3 markers) before every sleep is a power-off, until USB is plugged in
# Accelerometer (LIS3DH, address 0x18) register blocks: "REG=VALUE" pairs, applied in order, each read back.
: "${ACCEL_WAKE:=none}"          # tap, tilt or none: what wakes it after the power-off (USB plug always does);
                                # none powers the accelerometer down (off for now, user 2026-10-31)
: "${ACCEL_COMMON_REGS:=0x23=0x00 0x24=0x00 0x25=0x00}"   # +-2 g normal mode, no FIFO/latch, no INT2 functions
: "${ACCEL_CLEAR_REGS:=0x21=0x00 0x22=0x00 0x30=0x00 0x38=0x00}"    # all interrupt sources and filters off first (the chip keeps its state)
# double tap on Z: 400 Hz, +-8 g, click high-pass, 1.5 g, tap <= 30 ms, 2nd tap 100-300 ms later (docs/LIFELOG.md)
: "${ACCEL_TAP_REGS:=0x21=0x84 0x23=0x28 0x3A=0x98 0x3B=0x0C 0x3C=0x28 0x3D=0x50 0x38=0x20 0x22=0x80 0x20=0x77}"
: "${ACCEL_TILT_REGS:=0x32=0x20 0x33=0x05 0x30=0x20 0x22=0x40 0x20=0x57}"
: "${ACCEL_NONE_REGS:=0x20=0x00}"                         # power down: nothing but USB wakes it
: "${ACCEL_SAMPLE_REGS:=0x23=0x00 0x20=0x57}"             # enable measurement for the position reading
: "${ACCEL_SETTLE:=0.15}"        # seconds between enabling and reading the outputs

# ---- paths and commands (overridable for the host tests) ----
: "${LL_DATA:=/data}"
: "${LL_DIR:=$LL_DATA/lifelog}"
: "${PHOTO_DIR:=$LL_DATA/photos}"
: "${LIFELOG_CONF:=$LL_DATA/lifelog.conf}"
: "${OFF_FILE:=$LL_DATA/lifelog.off}"
: "${RTC_DIR:=/sys/class/rtc/rtc0}"
: "${KMSG:=/dev/kmsg}"
: "${PROC_UPTIME:=/proc/uptime}"
: "${VIDEO_NODE:=/dev/video0}"
: "${SPI_NODE:=/dev/spidev1.1}"
: "${DEVMEM:=devmem}"
: "${BATTERY_CMD:=magpie-battery}"
: "${SNAP_CMD:=snap}"
: "${GPSNAP_CMD:=gpsnap}"
: "${LED_CMD:=magpie-led}"
: "${ACCEL_CMD:=magpie-accel}"
: "${DMESG_CMD:=dmesg}"
: "${MOUNTPOINT_CMD:=mountpoint -q}"
: "${TIMEOUT_CMD:=timeout}"      # empty = run commands without a timeout
: "${SLEEP_CMD:=sleep}"
: "${POWEROFF_CMD:=poweroff -f}"
: "${POWEROFF_FALLBACK:=poweroff}"
: "${I2CGET:=i2cget}"
: "${I2CSET:=i2cset}"
: "${RMMOD_CMD:=rmmod}"
: "${MODPROBE_CMD:=modprobe}"
: "${I2C_DEV_DIR:=/sys/bus/i2c/devices}"
: "${I2C_NODE_DIR:=/dev}"
: "${ACCEL_ADDR:=0x18}"
: "${PIOC_PDSR:=0xfffff83c}"     # PIOC pin data status; bit 31 = PC31 = VBUS, active low
: "${GPBR1:=0xfffffe64}"
: "${GPBR2:=0xfffffe68}"
: "${GPBR3:=0xfffffe6c}"          # kernel fast-resume markers 0x4D5057xx (0 after power-on)
: "${HEALTHY_MARK:=0x4D500020}"
: "${UDC_FILE:=/sys/kernel/config/usb_gadget/magpie/UDC}"   # USB gadget binding: empty string = unbound
: "${PM_STATE:=/sys/power/state}"                          # echo mem = fast-resume sleep

ll_now() { date '+%Y-%m-%d %H:%M:%S'; }

ll_data_ok() { $MOUNTPOINT_CMD "$LL_DATA" > /dev/null 2>&1; }

# Seconds since kernel start (integer part).
ll_uptime() {
	_u=
	read -r _u _ < "$PROC_UPTIME" 2> /dev/null
	_u=${_u%%.*}
	case "$_u" in '' | *[!0-9]*) _u=0 ;; esac
	echo "$_u"
}

# ll_rotate FILE: move FILE to FILE.1 once it reaches LOG_MAX bytes.
ll_rotate() {
	[ -f "$1" ] || return 0
	_s=$(wc -c < "$1" 2> /dev/null)
	case "$_s" in '' | *[!0-9]*) return 0 ;; esac
	[ "$_s" -ge "$LOG_MAX" ] && mv -f "$1" "$1.1" 2> /dev/null
	return 0
}

# ll_ensure_dir: create the lifelog directory if /data is mounted.
ll_ensure_dir() {
	ll_data_ok || return 1
	[ -d "$LL_DIR" ] || mkdir -p "$LL_DIR" 2> /dev/null
	[ -d "$LL_DIR" ]
}

# ll_log MESSAGE...: kernel log, plus /data/lifelog/daemon.log when /data is mounted.
ll_log() {
	echo "${LL_TAG:-magpie-lifelogd}: $*" > "$KMSG" 2> /dev/null
	if ll_ensure_dir; then
		ll_rotate "$LL_DIR/daemon.log"
		echo "$(ll_now) ${LL_TAG:-magpie-lifelogd}: $*" >> "$LL_DIR/daemon.log" 2> /dev/null
	fi
	return 0
}

# ll_int NAME DEFAULT: reset NAME to DEFAULT unless it is an (optionally negative) integer.
ll_int() {
	eval "_v=\${$1}"
	case "${_v#-}" in
	'' | *[!0-9]*)
		ll_log "config: $1='$_v' is not a number, using $2"
		eval "$1=\$2"
		;;
	esac
}

# ll_load_conf: read KEY=VALUE lines from LIFELOG_CONF. Only keys that exist above are accepted;
# a value is taken literally (never executed). '#' starts a comment; quotes around the value are removed.
ll_load_conf() {
	if [ -f "$LIFELOG_CONF" ]; then
		while IFS= read -r _line || [ -n "$_line" ]; do
			_line=${_line%%#*}
			case "$_line" in *=*) ;; *) continue ;; esac
			_k=${_line%%=*}
			_v=${_line#*=}
			_k=${_k#"${_k%%[![:space:]]*}"}
			_k=${_k%"${_k##*[![:space:]]}"}
			_v=${_v#"${_v%%[![:space:]]*}"}
			_v=${_v%"${_v##*[![:space:]]}"}
			case "$_v" in
			\"*\") _v=${_v#\"}; _v=${_v%\"} ;;
			\'*\') _v=${_v#\'}; _v=${_v%\'} ;;
			esac
			case "$_k" in '' | *[!A-Z0-9_]*) continue ;; esac
			if eval "[ \"\${$_k+set}\" = set ]"; then
				eval "$_k=\$_v"
			else
				ll_log "config: unknown key $_k ignored"
			fi
		done < "$LIFELOG_CONF"
	fi
	for _p in INTERVAL:30 MIN_ALARM:8 BOOT_PREAMBLE:5 VBUS_CHECKS:3 OFF_LEVEL:205 WARN_LEVEL:207 \
		DARK_JPEG_BYTES:60000 DARK_AFTER:2 DARK_INTERVAL:120 DARK_LONG_AFTER:10 DARK_LONG_INTERVAL:480 \
		FACEDOWN_SIGN:-1 FACEDOWN_THRESHOLD:800 FACEDOWN_OTHER_MAX:350 FACEDOWN_INTERVAL:120 \
		FACEDOWN_LONG_AFTER:10 FACEDOWN_LONG_INTERVAL:240 LOG_MAX:1048576 WAIT_TIMEOUT:15 DATA_WAIT:20 \
		SNAP_TIMEOUT:30 GPSNAP_TIMEOUT:30 NOALARM_RETRY:30 COLD_EVERY:70 RESUME_MAX_FAIL:3 GPS:1 BLINK:1 BLINK_LED:1 BLINK_LEVEL:1; do
		ll_int "${_p%%:*}" "${_p#*:}"
	done
	[ "$VBUS_CHECKS" -ge 1 ] || VBUS_CHECKS=1
	case "$FACEDOWN_AXIS" in '' | x | y | z) ;; *)
		ll_log "config: FACEDOWN_AXIS='$FACEDOWN_AXIS' must be x, y or z; face-down detection off"
		FACEDOWN_AXIS=
		;;
	esac
	case "$SLEEP_MODE" in resume | poweroff) ;; *)
		ll_log "config: SLEEP_MODE='$SLEEP_MODE' must be resume or poweroff; using resume"
		SLEEP_MODE=resume
		;;
	esac
	case "$ACCEL_WAKE" in tap | tilt | none) ;; *)
		ll_log "config: ACCEL_WAKE='$ACCEL_WAKE' must be tap, tilt or none; using none"
		ACCEL_WAKE=none
		;;
	esac
}
