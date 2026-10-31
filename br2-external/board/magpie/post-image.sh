#!/bin/sh
# Buildroot post-image hook: wrap the kernel (zImage with the DTB appended) as the legacy uImage the stock
# U-Boot boots, then fail the build if it is larger than the 2 MiB U-Boot reads from eMMC
# (mmc read 20100000 10 1000). mkimage comes from the build container (u-boot-tools), so Buildroot doesn't
# have to build host-uboot-tools and its gnutls chain. Header values as before: ARM Linux kernel,
# uncompressed wrapper, load/entry 0x20008000, name "Linux-<version>".
# $1 = images dir; BUILD_DIR comes from Buildroot.
set -eu
LIMIT=2097152
DIR=${1:?images dir}
ZIMG=$(ls "$DIR"/zImage.* 2>/dev/null | head -n1 || true)
[ -n "$ZIMG" ] && [ -f "$ZIMG" ] || { echo "post-image: no zImage.<dtb> in $DIR" >&2; exit 1; }
ver=$(ls -d "${BUILD_DIR:?}"/linux-[0-9]* | head -n1 | sed 's|.*/linux-||')
IMG="$DIR/uImage.${ZIMG##*/zImage.}"
mkimage -A arm -O linux -T kernel -C none -a 0x20008000 -e 0x20008000 -n "Linux-$ver" -d "$ZIMG" "$IMG" > /dev/null
size=$(wc -c < "$IMG")
echo "post-image: $IMG is $size bytes (limit $LIMIT, headroom $((LIMIT - size)))"
if [ "$size" -gt "$LIMIT" ]; then
    echo "post-image: ERROR: uImage exceeds $LIMIT bytes" >&2
    exit 1
fi
