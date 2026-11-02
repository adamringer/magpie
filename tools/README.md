# tools/

Helper programs for magpie, the custom firmware for the Narrative Clip 1. Most of them run on the host PC the Clip is
plugged into over USB. A few run on the Clip itself (marked below). Nothing here is needed to build the firmware; see
the top-level [README](../README.md) for building, the backup before flashing, and the flashing and recovery
procedures. This file only describes the tools.

Over USB the Clip is a network gadget at 192.168.2.2 (the host side is 192.168.2.1). It runs telnet and FTP, and has no
ssh. Most link tools talk to those two services.

## Prerequisites

- **Repo venv.** Run `uv sync` once in the repo root. The Python tools are then run as `.venv/bin/python -I ...`
  from the repo root (the examples below use this form).
- **System python3 with numpy, scipy and matplotlib.** The GPS analysis scripts (`gps/`) and the phone client use the
  system `python3` (the examples show `python3 -I`). `phone/usbclient.py` needs only pyusb (`python3-usb`).
- **Root.** Needed for: `ecm_tap_bridge.py` (and `bridge.sh start`), `flash_ums.sh`, `install-privileged.sh`.
  `install-privileged.sh` sets up passwordless sudo for the bridge and the flash script only. USB access for
  `samba.py`, `restore.py` and `phone/usbclient.py` needs root or a udev rule.
- Files with locations in them (GPS results, seed positions) belong under `analysis/`, which is git-ignored.

## Link and shell

On some hosts the kernel's cdc_ether driver keeps the Clip's USB Ethernet link down (it waits for a link
notification that does not arrive intact). There a small userspace bridge replaces it; where the kernel link comes up
(e.g. a NetworkManager profile with 192.168.2.1 on the gadget interface), no bridge is needed.

- `ecm_tap_bridge.py` - userspace USB-Ethernet to TAP bridge. It detaches the kernel's cdc_ether driver, talks to the
  bulk endpoints directly and passes frames to a TAP interface. Needs root. Ctrl-C restores the kernel driver.
  `sudo .venv/bin/python -I -B tools/ecm_tap_bridge.py`
- `bridge.sh` - starts, stops or checks that bridge in the background, and reports the Clip's USB mode (ethernet,
  mass-storage, vendor or none) and whether it answers ping. Asks for sudo unless `install-privileged.sh` was run.
  `tools/bridge.sh start|stop|restart|status`
- `magpie-bridge` - the root-run wrapper that `install-privileged.sh` installs for the bridge. Not run from the repo.
  Installed as `magpie-bridge start|stop|status`.
- `magpie_sh.py` - runs shell commands on the Clip over telnet (root shell, no login) and prints each command's output.
  `.venv/bin/python -I -W ignore tools/magpie_sh.py 'uname -a' 'cat /proc/mtd'`
- `magpie_ftp_get.py` - downloads a file from the Clip's FTP server, or the first N bytes of a device node.
  `.venv/bin/python -I tools/magpie_ftp_get.py REMOTE LOCAL [--limit BYTES]`
- `magpie_ftp_put.py` - uploads a file to the Clip over FTP (the server runs as root and is writable, so it can
  overwrite system files in RAM or on `/data`).
  `.venv/bin/python -I tools/magpie_ftp_put.py local_file.txt /tmp/remote_file.txt`
- `magpie_timesync.py` - sets the docked Clip's system clock and RTC from the host's NTP-synchronised clock, gives the
  Real-time Timer (RTT) a known epoch and calibrates it, then reports the remaining offsets. This writes the Clip's
  clocks and appends to files under `/data/time/`. `--check` only measures, `--wait` retries the connection for up to
  120 s (for use in a docking hook). The GPS tools use the RTT epoch to turn capture counts into UTC.
  `.venv/bin/python -I -W ignore tools/magpie_timesync.py [--check] [--wait]`

## Flashing and recovery

Read the top-level README first. Back up the original firmware before using any of these.

- `samba.py` - client for the SoC's boot-ROM monitor (SAM-BA) over USB serial (`03eb:6124`). Sub-commands: `version`,
  `read32`, `write32`, `bootmode`, `set-bootmode`, `reset`, `dump`, `load`, `go`. It is a library for `restore.py` too.
  **It can write RAM and chip registers, change the boot mode and reset the chip.**
  `.venv/bin/python -I tools/samba.py version`
- `restore.py` - the recovery path when the Clip is in SAM-BA mode. `sdram` loads and runs the SDRAM-init applet,
  `memtest` checks the 32 MB of SDRAM, `uboot` initialises SDRAM and runs a small U-Boot from RAM that exports the
  Clip's eMMC as a USB disk. Nothing is written to flash by this tool itself, but the Clip is left running U-Boot.
  `.venv/bin/python -I -B tools/restore.py sdram|memtest|uboot`
- `flash_ums.sh` - **writes the eMMC.** With the Clip in U-Boot's USB disk mode it writes the kernel to partition 1 and
  the root filesystem to partition 2. `--restore-stock` writes the backup of the first 32 MiB instead. It refuses
  unless the device is a removable disk of the expected size with partitions 1 and 2 and nothing mounted.
  `--dry-run` runs all checks and writes nothing (no root needed); `--eject` ejects the drive afterwards, which makes
  U-Boot reset into a normal boot. Check the device name twice.
  `sudo tools/flash_ums.sh [--dry-run] [--restore-stock] [--eject] /dev/sdX`
- `install-privileged.sh` - **one-time root setup, changes the host system.** Installs root-owned copies of the bridge
  and flash script under `/usr/local/` and a sudoers rule that lets one user run exactly those two commands without a
  password. Needs the system package `python3-usb`. Re-run it after changing either script. The script's header says how
  to remove it.
  `sudo tools/install-privileged.sh [user]`
- `magpie_dump.py` - reads a block device of the running Clip into a local file in 4 MiB chunks, via a temporary file
  in the Clip's RAM disk and FTP, checking each chunk's MD5. Read-only for the flash; this is the backup tool.
  `.venv/bin/python -I -W ignore tools/magpie_dump.py /dev/mmcblk0 backup/mmcblk0_first32M.bin --mib 32`

## Debug

- `serial_log.py` - captures the Clip's debug serial port (115200 8N1) to a log file and the screen. Defaults to the
  first `/dev/ttyUSB*` or `/dev/ttyACM*`, 60 s, and a time-stamped file under `analysis/`.
  `.venv/bin/python -I -B tools/serial_log.py --seconds 60 [--port /dev/ttyUSB0] [--out analysis/serial.log]`
- `yuv2png.py` - converts a raw YUYV or UYVY frame (as written by the Clip's `snap` command) to PNG. The format comes
  from the file extension, the size defaults to 640x480, and the conversion is full-range BT.601.
  `.venv/bin/python -I tools/yuv2png.py photo.yuyv [-o photo.png] [--size 640x480] [--format yuyv|uyvy]`

## Phone and WebUSB

These speak the Clip's vendor-specific USB data interface (protocol v1, described in
[docs/PHONE_USB.md](../docs/PHONE_USB.md)). Only that interface is claimed, so the Ethernet link keeps working.

- `phone/usbclient.py` - command-line client and reference implementation for the browser client. Commands: `status`,
  `list`, `get`, `hash`, `delete`, `time`, `settime`, `bench`; `--json` gives raw JSON. **`delete` removes photos on the
  Clip** (it hashes each file first and deletes only if it still matches; use `get` first for a copy, and `--yes`
  skips the confirmation). **`settime` sets the Clip's clocks.**
  `python3 -I tools/phone/usbclient.py get --all --dest photos_copy`
- `phone/getdiag.py` - test helper: downloads many files the way `usbclient.py get` does and compares them with
  reference copies fetched over FTP. Bad copies are kept as `OUTDIR/<name>.bad`, with the first difference described.
  `/usr/bin/python3 -I tools/phone/getdiag.py REFDIR OUTDIR [ROUNDS]`
- `phone/stress.sh` - test helper: runs `getdiag.py` while FTP uploads flood the network link, to check phone-interface
  downloads stay intact. **It repeatedly uploads a 2 MB junk file to the Clip's `/tmp`** (deleted at the end).
  `tools/phone/stress.sh ROUNDS REFDIR OUTDIR`

## GPS

Tools for turning the Clip's raw GPS captures (`.snap`, with a `.time` sidecar) into position fixes on the host. See
[docs/GPS_PROCESSING.md](../docs/GPS_PROCESSING.md) for the method. They use the system `python3` (numpy, scipy,
matplotlib). Results contain locations: keep seed files, logs, KMLs and plots under `analysis/`.

Typical run:

1. `gps/pull_snaps.sh` copies the captures of a time window from the docked Clip over FTP.
2. `gps/process_run.sh` fetches the broadcast ephemeris, runs the blind search, the run-level solve, the aided search
   and the final solves, and writes logs and KML files.
3. `gps/cep_plot.py` plots the fixes as east/north error around a reference and prints CEP50/CEP95.

```
tools/gps/pull_snaps.sh 20261101-140000 20261101-143000 analysis/run1
tools/gps/process_run.sh analysis/run1 analysis/seed.txt [analysis/ref.txt] [512]
python3 -I tools/gps/cep_plot.py analysis/ref.txt analysis/run1/cep.png "run 1" \
    analysis/run1/fixes_blind_512.kml analysis/run1/fixes_aided_512.kml
```

For weak or indoor signals, where no satellite is strong enough for any fix, use `gps/posaided.py` instead. It only
says whether a signal is present; it computes no position.

Scripts you run:

- `gps/pull_snaps.sh` - copies the `.snap` captures (and `.time` sidecars, optionally the photo) whose file-name time
  (UTC) is in a window from the Clip's FTP server into a local directory. `SRC_DIR` defaults to `/data/photos`;
  `CLIP` in the environment overrides the address. Read-only on the Clip.
  `tools/gps/pull_snaps.sh FROM TO DEST [SRC_DIR] [--jpg]` (times as `YYYYMMDD-HHMMSS`)
- `gps/process_run.sh` - the whole pipeline for one directory of captures. SEED is a file with "lat lon" of the last
  known position (within a few hundred metres), REF an optional true position for distance reports, MS the capture
  length to use (default 512).
  `tools/gps/process_run.sh DIR SEED [REF] [MS]`
- `gps/snapfix.py` - one position fix from one capture, with coarse-time navigation: acquisition, a Doppler fix, then a
  code-phase fix. `--lengths` only measures (used by `process_run.sh`), `--meas-out`/`--meas-in` save and reuse
  measurements. Also used as a library.
  `python3 -I tools/gps/snapfix.py CAPTURE.snap brdcDDD0.YYn.gz \
    [--prn 26,9,3] [--utc 'YYYY-MM-DD HH:MM:SS'] [--out FILE]`
- `gps/joint.py` - run-level solve: one shared clock offset for all captures of a run, taken from the captures that
  solve alone, then every capture again with that as a prior. `--kml` writes the fixes as KML.
  `python3 -I tools/gps/joint.py MEAS.jsonl brdcDDD0.YYn.gz --ms 1024 \
    [--zmin 8] [--sigma 0.5] [--ref FILE] [--out FILE]`
- `gps/aided.py` - finds weak satellites by predicting where they must be (from a seed position and the strong
  satellites' clock calibration) and searching only there. `--null` searches below the horizon to set the threshold.
  `python3 -I tools/gps/aided.py MEAS.jsonl NAV --dir CAPTURE_DIR --seed-file SEED --ms N \
    [--meas-out OUT.jsonl] [--null]`
- `gps/posaided.py` - position-aided collective detection for signals too weak for the above. Reports, per capture, the
  collective peak against the null peaks and the best time and code-bias corrections.
  `python3 -I tools/gps/posaided.py NAV SEED CAP.snap [CAP.snap ...] [--ms 512] [--fc -115] [--fwin 50]`
- `gps/cep_plot.py` - error scatter plot from the blind and aided KML files, with 25/50/100 m rings and CEP50/CEP95.
  Missing KMLs are skipped.
  `python3 -I tools/gps/cep_plot.py REF OUT.png TITLE BLIND.kml AIDED.kml`
- `gps/doc_figures.py` - regenerates the figures in `docs/GPS_PROCESSING.md` from one capture. The geometry and
  fix-scatter figures are synthetic, so no real position is in the output.
  `python3 -I tools/gps/doc_figures.py CAPTURE.snap OUTDIR`
- `gps/nav.py` - fetches GPS broadcast ephemeris (a public CORS file first, an IGS mirror as fallback) and converts
  RINEX 3 to RINEX 2. **It needs internet access.** Also a library.
  `python3 -I tools/gps/nav.py fetch YYYY DDD [OUTDIR]`, or
  `python3 -I tools/gps/nav.py convert IN.rnx[.gz] OUT.YYn[.gz]`
- `gps/kml.py` - writes KML placemarks. Also a library.
  `python3 -I tools/gps/kml.py OUT.kml NAME LAT LON [DESCRIPTION]`

Run on the Clip:

- `gps/sky4s.sh` - **runs on the Clip.** Takes a series of long (4096 ms) GPS captures for an open-sky test, with no
  camera and no fast resume, into `/data/gps/sky4s/` with a one-line log. It **stops the lifelog and
  charge-LED daemons for the run and restarts them at the end**, and flashes the LEDs.
  Copy it to the Clip first (for example with `magpie_ftp_put.py`) and start it detached.
  Then fetch the captures with `pull_snaps.sh ... /data/gps/sky4s`.
  `start-stop-daemon -S -b -x /data/sky4s.sh -- COUNT PERIOD_S DELAY_S` (defaults 10, 30, 60)

Shared modules (imported by the scripts above; the first two are not run on their own):

- `gps/drift_search.py` - the acquisition engine: a search in which the code drift is tied to the carrier offset.
  Imported by `snapfix.py`, `aided.py`, `posaided.py` and `doc_figures.py`.
- `gps/gpsca.py` - the GPS L1 C/A spreading codes, used by `drift_search.py`.
- `gps/snapfix.py` - also imported by `joint.py`, `aided.py` and `posaided.py`.
- `gps/nav.py` - also imported by `snapfix.py`.
- `gps/kml.py` - imported by `joint.py` for `--kml`.

## Misc

- `web/make_logo.py` - draws the magpie logo (a magpie whose eye is a camera lens) and writes the SVG and PNG files for
  the README and the web page, deterministically. Needs pycairo (system package `python3-cairo`).
  `python3 -I tools/web/make_logo.py`
