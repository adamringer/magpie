#!/usr/bin/env bash
# Flash the new kernel + rootfs to the Clip while U-Boot exports its eMMC as a USB disk (ums).
#
#   sudo tools/flash_ums.sh /dev/sdX                  # write uImage to p1, squashfs to p2
#   tools/flash_ums.sh --dry-run /dev/sdX             # all checks, no writes (no root needed)
#   sudo tools/flash_ums.sh --restore-stock /dev/sdX  # write backup/mmcblk0_first32M.bin to the first 32 MiB
#   --eject: eject the drive after a successful write (U-Boot then resets into a normal boot)
#
# Refuses to write unless the device is a removable 15269888-sector "UMS disk" with
# partitions 1 and 2, and nothing on it is mounted. Afterwards: sudo eject /dev/sdX
# (U-Boot then resets into a normal boot). Get into ums mode with tools/restore.py uboot.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL="$REPO/output/images/uImage.at91sam9g25-magpie"
ROOTFS="$REPO/output/images/rootfs.squashfs"
STOCK="$REPO/backup/mmcblk0_first32M.bin"
WANT_SECTORS=15269888
KERNEL_MAX=2097152          # U-Boot reads exactly 0x1000 sectors
STOCK_SIZE=$((32 * 1024 * 1024))

die() { echo "flash_ums: error: $*" >&2; exit 1; }
usage() { echo "usage: $0 [--dry-run] [--restore-stock] [--eject] /dev/sdX" >&2; exit 2; }

dry=0 stock=0 eject=0 dev=
for a in "$@"; do
    case "$a" in
        --dry-run) dry=1 ;;
        --restore-stock) stock=1 ;;
        --eject) eject=1 ;;
        -h|--help) usage ;;
        -*) echo "unknown option: $a" >&2; usage ;;
        *) [ -z "$dev" ] || usage; dev="$a" ;;
    esac
done
[ -n "$dev" ] || usage

[[ "$dev" =~ ^/dev/(sd[a-z]+)$ ]] || die "'$dev' is not of the form /dev/sdX"
name="${BASH_REMATCH[1]}"
[ -b "$dev" ] || die "$dev does not exist or is not a block device"
sys="/sys/block/$name"
[ -d "$sys" ] || die "$sys not found"

# --- identity checks ---
[ "$(cat "$sys/removable")" = 1 ] || die "$dev is not removable"
sectors="$(cat "$sys/size")"
[ "$sectors" = "$WANT_SECTORS" ] || die "$dev has $sectors sectors, expected $WANT_SECTORS"
model="$(tr -s ' ' < "$sys/device/model" 2>/dev/null | sed 's/ *$//')" || model=
vendor="$(tr -s ' ' < "$sys/device/vendor" 2>/dev/null | sed 's/ *$//')" || vendor=
case "$model" in
    *"UMS disk"*) ;;
    *) die "$dev model is '$model' (vendor '$vendor'), expected U-Boot's 'UMS disk'" ;;
esac
[ -b "${dev}1" ] && [ -b "${dev}2" ] || die "${dev}1 and ${dev}2 must both exist"
if grep -q "^${dev}" /proc/mounts; then
    grep "^${dev}" /proc/mounts >&2
    die "something on $dev is mounted; unmount it first"
fi
echo "identity OK: $dev vendor='$vendor' model='$model' sectors=$sectors"

part_bytes() { echo $(( $(cat "/sys/class/block/${1#/dev/}/size") * 512 )); }
fsize() { stat -c %s "$1"; }

# Write a file to a device, flush, verify with cmp, print its sha256.
write_verify() {
    local src="$1" dst="$2" size
    size="$(fsize "$src")"
    echo "writing $(basename "$src") ($size bytes) -> $dst"
    dd if="$src" of="$dst" bs=1M conv=fsync status=none
    blockdev --flushbufs "$dst"
    cmp -n "$size" "$src" "$dst" || die "verify FAILED for $dst"
    echo "verified $dst: sha256 $(sha256sum "$src" | cut -d' ' -f1)"
}

if [ "$stock" = 1 ]; then
    [ -f "$STOCK" ] || die "missing $STOCK"
    [ "$(fsize "$STOCK")" = "$STOCK_SIZE" ] || die "$STOCK is not 32 MiB"
    echo "plan: write stock first 32 MiB (MBR + p1 + p2) to $dev"
    if [ "$dry" = 1 ]; then echo "dry run: nothing written"; exit 0; fi
    [ "$EUID" = 0 ] || die "run as root (sudo)"
    write_verify "$STOCK" "$dev"
else
    [ -f "$KERNEL" ] || die "missing $KERNEL (build first)"
    [ -f "$ROOTFS" ] || die "missing $ROOTFS (build first)"
    ksize="$(fsize "$KERNEL")" rsize="$(fsize "$ROOTFS")"
    p1="$(part_bytes "${dev}1")" p2="$(part_bytes "${dev}2")"
    [ "$ksize" -le "$KERNEL_MAX" ] || die "uImage is $ksize bytes, limit $KERNEL_MAX"
    [ "$ksize" -le "$p1" ] || die "uImage ($ksize) does not fit ${dev}1 ($p1 bytes)"
    [ "$rsize" -le "$p2" ] || die "rootfs ($rsize) does not fit ${dev}2 ($p2 bytes)"
    echo "plan: uImage $ksize bytes -> ${dev}1 ($p1 bytes); rootfs $rsize bytes -> ${dev}2 ($p2 bytes)"
    if [ "$dry" = 1 ]; then echo "dry run: nothing written"; exit 0; fi
    [ "$EUID" = 0 ] || die "run as root (sudo)"
    write_verify "$KERNEL" "${dev}1"
    write_verify "$ROOTFS" "${dev}2"
fi

if [ "$eject" = 1 ]; then
    eject "$dev" && echo "done, ejected $dev (U-Boot resets into a normal boot)"
else
    echo "done. Now: sudo eject $dev   (U-Boot resets into a normal boot)"
fi
