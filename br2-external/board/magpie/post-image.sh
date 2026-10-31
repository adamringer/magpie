#!/bin/sh
# Buildroot post-image hook: fail the build if the kernel uImage is larger
# than the 2 MiB the stock U-Boot reads from eMMC (mmc read 20100000 10 1000).
# $1 = images dir.
set -eu

LIMIT=2097152
IMG=$(ls "${1:?images dir}"/uImage "$1"/uImage.* 2>/dev/null | head -n1 || true)

[ -n "$IMG" ] && [ -f "$IMG" ] || { echo "post-image: no uImage in $1" >&2; exit 1; }
size=$(wc -c < "$IMG")
echo "post-image: $IMG is $size bytes (limit $LIMIT, headroom $((LIMIT - size)))"
if [ "$size" -gt "$LIMIT" ]; then
    echo "post-image: ERROR: uImage exceeds $LIMIT bytes" >&2
    exit 1
fi
