#!/bin/sh
# Regenerate linux.config (a savedefconfig) from allconfig.txt: start from
# "make allnoconfig" so nothing is built that is not listed there.
# Needs the Buildroot kernel build dir (after a first build) and the container:
#   scripts/xrun.sh br2-external/board/magpie/linux/regen-config.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
k=$(ls -d /work/output/build/linux-[0-9]* | head -n1)
mk="make -C $k ARCH=arm CROSS_COMPILE=/work/output/host/bin/arm-linux-"
$mk KCONFIG_ALLCONFIG="$here/allconfig.txt" allnoconfig
$mk savedefconfig
cp "$k/defconfig" "$here/linux.config"
