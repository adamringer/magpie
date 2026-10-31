#!/usr/bin/env bash
# Run a command in the Buildroot container, with the cross toolchain
# (output/host/bin) on PATH and the repo mounted at /work.
#   scripts/xrun.sh make -C restore
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="magpie-br:$(sed -n 's/^BR_VERSION=//p' "$ROOT/scripts/br.sh")"

docker image inspect "$IMAGE" >/dev/null 2>&1 \
    || { echo "no image $IMAGE; run scripts/br.sh first" >&2; exit 1; }

tty_flag=-i
[ -t 0 ] && tty_flag=-it

exec docker run --rm "$tty_flag" -v "$ROOT:/work" -w /work \
    -e PATH="/work/output/host/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    "$IMAGE" "$@"
