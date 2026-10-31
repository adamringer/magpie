#!/usr/bin/env bash
# One-time setup so the flash and bridge can run without a password prompt.
# Run as root, after `sudo apt install python3-usb`:
#   sudo tools/install-privileged.sh [user]      (user defaults to $SUDO_USER)
#
# Installs root-owned copies (nothing user-writable ever runs as root):
#   /usr/local/sbin/magpie-flash-ums       tools/flash_ums.sh, pinned to this repo's images
#   /usr/local/sbin/magpie-bridge          tools/magpie-bridge
#   /usr/local/lib/magpie/ecm_tap_bridge.py
# and /etc/sudoers.d/magpie allowing that user exactly those two commands without a password.
# Re-run it after changing either script. Remove with: rm /etc/sudoers.d/magpie /usr/local/sbin/magpie-*
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
user="${1:-${SUDO_USER:-}}"
[ -n "$user" ] && id "$user" > /dev/null || { echo "usage: sudo $0 <user>" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

/usr/bin/python3 -I -c 'import usb.core' 2> /dev/null \
    || { echo "missing system pyusb: run 'sudo apt install python3-usb' first" >&2; exit 1; }

install -d -o root -g root -m 0755 /usr/local/lib/magpie
install -o root -g root -m 0644 "$REPO/tools/ecm_tap_bridge.py" /usr/local/lib/magpie/ecm_tap_bridge.py
install -o root -g root -m 0755 "$REPO/tools/magpie-bridge" /usr/local/sbin/magpie-bridge

# The flash script finds its images relative to itself; pin the installed copy to this repo.
tmp="$(mktemp)"
sed "s|^REPO=.*|REPO=\"$REPO\"  # pinned by install-privileged.sh|" "$REPO/tools/flash_ums.sh" > "$tmp"
grep -q "^REPO=\"$REPO\"" "$tmp" || { echo "could not pin REPO in flash_ums.sh" >&2; rm -f "$tmp"; exit 1; }
install -o root -g root -m 0755 "$tmp" /usr/local/sbin/magpie-flash-ums
rm -f "$tmp"

rule="$user ALL=(root) NOPASSWD: /usr/local/sbin/magpie-flash-ums, /usr/local/sbin/magpie-bridge"
tmp="$(mktemp)"
printf '# magpie tools (tools/install-privileged.sh)\n%s\n' "$rule" > "$tmp"
visudo -cf "$tmp" > /dev/null || { echo "sudoers syntax check failed" >&2; rm -f "$tmp"; exit 1; }
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/magpie
rm -f "$tmp"

echo "installed:"
ls -l /usr/local/sbin/magpie-flash-ums /usr/local/sbin/magpie-bridge /usr/local/lib/magpie/ecm_tap_bridge.py /etc/sudoers.d/magpie
echo "sudoers: $rule"
