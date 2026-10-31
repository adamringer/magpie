#!/usr/bin/env bash
# Start, stop or check the USB Ethernet bridge to the Clip (tools/ecm_tap_bridge.py).
#
#   tools/bridge.sh start     # background, asks for sudo; log in analysis/bridge.log
#   tools/bridge.sh stop      # same as Ctrl-C: releases the Clip, reattaches cdc_ether
#   tools/bridge.sh restart
#   tools/bridge.sh status
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
BRIDGE="$REPO/tools/ecm_tap_bridge.py"
LOG="$REPO/analysis/bridge.log"
# Installed by tools/install-privileged.sh: runs without a password prompt.
PRIV=/usr/local/sbin/magpie-bridge
[[ -x "$PRIV" ]] && LOG=/run/magpie-bridge.log
MAGPIE_IP=192.168.2.2

# Matches the python process only, not the sudo wrapper, so stop sends one SIGINT.
bridge_pid() {
    pgrep -f '^[^ ]*/python[0-9.]* .*ecm_tap_bridge\.py' || true
}

usb_mode() {
    if lsusb -d 0525:a4a1 >/dev/null 2>&1; then echo ethernet
    elif lsusb -d 0525:a4a5 >/dev/null 2>&1; then echo mass-storage
    elif lsusb -d f0f0:1337 >/dev/null 2>&1; then echo vendor
    else echo none
    fi
}

status() {
    local pid
    pid="$(bridge_pid)"
    echo "Camera USB mode: $(usb_mode)"
    if [[ -n "$pid" ]]; then echo "bridge: running (pid $pid)"; else echo "bridge: not running"; fi
    if ping -c1 -W2 "$MAGPIE_IP" >/dev/null 2>&1; then echo "ping $MAGPIE_IP: ok"; else echo "ping $MAGPIE_IP: no answer"; fi
}

start() {
    if [[ -n "$(bridge_pid)" ]]; then
        echo "bridge already running (pid $(bridge_pid))"
        return 0
    fi
    local mode
    mode="$(usb_mode)"
    if [[ "$mode" != ethernet ]]; then
        echo "Camera isn't in Ethernet mode (USB mode: $mode); not starting" >&2
        return 1
    fi
    if [[ -x "$PRIV" ]]; then
        sudo -n "$PRIV" start
    else
        mkdir -p "$(dirname "$LOG")"
        # -B: no root-owned .pyc files in .venv
        sudo -b "$PY" -I -B "$BRIDGE" >"$LOG" 2>&1
    fi
    # Up once the bridge reports it or the Clip answers a ping.
    for _ in $(seq 1 20); do
        if grep -q '^bridging' "$LOG" 2>/dev/null || ping -c1 -W1 "$MAGPIE_IP" >/dev/null 2>&1; then
            status
            return 0
        fi
        sleep 0.5
    done
    echo "bridge didn't come up; log:" >&2
    cat "$LOG" >&2
    return 1
}

stop() {
    local pid
    pid="$(bridge_pid)"
    if [[ -z "$pid" ]]; then
        echo "bridge not running"
        return 0
    fi
    if [[ -x "$PRIV" ]]; then sudo -n "$PRIV" stop; else sudo kill -INT $pid; fi
    for _ in $(seq 1 20); do
        [[ -z "$(bridge_pid)" ]] && { echo "bridge stopped"; return 0; }
        sleep 0.5
    done
    echo "bridge still running (pid $(bridge_pid)); try again or unplug the camera" >&2
    return 1
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop && start ;;
    status) status ;;
    *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
