#!/bin/sh
# pull_snaps.sh FROM TO DEST [SRC_DIR] [--jpg]
# Copy the Clip's GPS captures (.snap + .time sidecar, optionally the photo) whose file-name time
# (YYYYMMDD-HHMMSS, the Clip's UTC clock) lies in [FROM, TO] into DEST, over FTP from the docked Clip.
#   FROM/TO: YYYYMMDD-HHMMSS (UTC), e.g. 20261101-140000 20261101-143000
#   SRC_DIR: directory on the Clip, default /data/photos (lifelog); /data/gps/sky4s for test runs
# Run on the host the Clip is docked to. CLIP (default 192.168.2.2) selects the address.
set -eu
[ $# -ge 3 ] || { sed -n '2,8p' "$0"; exit 1; }
FROM=$1; TO=$2; DEST=$3; SRC=/data/photos; JPG=0
shift 3
for a in "$@"; do
	case "$a" in
	--jpg) JPG=1 ;;
	*) SRC=$a ;;
	esac
done
CLIP=${CLIP:-192.168.2.2}
mkdir -p "$DEST"
n=0
for f in $(curl -s --list-only "ftp://$CLIP$SRC/" | grep -E '^[0-9]{8}-[0-9]{6}\.snap$' | sort); do
	stem=${f%.snap}
	[ "$stem" \< "$FROM" ] && continue
	[ "$stem" \> "$TO" ] && continue
	curl -s -f -o "$DEST/$f" "ftp://$CLIP$SRC/$f"
	curl -s -f -o "$DEST/$f.time" "ftp://$CLIP$SRC/$f.time" || rm -f "$DEST/$f.time"
	[ "$JPG" = 1 ] && { curl -s -f -o "$DEST/$stem.jpg" "ftp://$CLIP$SRC/$stem.jpg" || rm -f "$DEST/$stem.jpg"; }
	n=$((n + 1))
done
t=$(ls "$DEST"/*.snap.time 2> /dev/null | wc -l)
echo "$n captures in $DEST ($t with a .time sidecar)"
