#!/usr/bin/env bash
# Run Buildroot in Docker. Usage: scripts/br.sh [make args...]
#   scripts/br.sh magpie_defconfig
#   scripts/br.sh menuconfig
#   scripts/br.sh
# BR_OUTPUT picks the output dir (default "output"), e.g. the U-Boot restore build:
#   BR_OUTPUT=output-restore scripts/br.sh magpie_restore_defconfig
#   BR_OUTPUT=output-restore scripts/br.sh
set -euo pipefail

BR_VERSION=2026.08.1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The tag includes a hash of the Dockerfile, so editing it rebuilds the image.
IMAGE="magpie-br:${BR_VERSION}-$(sha256sum "$ROOT/docker/Dockerfile" | cut -c1-8)"
TARBALL="buildroot-${BR_VERSION}.tar.xz"
BASE_URL="https://buildroot.org/downloads"
BR_OUTPUT="${BR_OUTPUT:-output}"

cd "$ROOT"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" \
        --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" docker
fi

if [ ! -f buildroot/Makefile ]; then
    mkdir -p dl
    tmp="$(mktemp -d "$ROOT/dl/fetch.XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    curl -fL -o "$tmp/$TARBALL" "$BASE_URL/$TARBALL"
    curl -fL -o "$tmp/$TARBALL.sign" "$BASE_URL/$TARBALL.sign"
    want="$(sed -n 's/^SHA256: *\([0-9a-f]\{64\}\)  *'"$TARBALL"'$/\1/p' "$tmp/$TARBALL.sign")"
    [ -n "$want" ] || { echo "no SHA256 line in $TARBALL.sign" >&2; exit 1; }
    echo "$want  $tmp/$TARBALL" | sha256sum -c -
    mkdir -p buildroot
    tar -xf "$tmp/$TARBALL" -C buildroot --strip-components=1
fi

tty_flag=-i
[ -t 0 ] && tty_flag=-it

exec docker run --rm "$tty_flag" \
    -v "$ROOT:/work" \
    "$IMAGE" \
    make -C /work/buildroot O=/work/$BR_OUTPUT BR2_EXTERNAL=/work/br2-external \
        BR2_DL_DIR=/work/dl "$@"
