# Phone download over WebUSB: USB interface and protocol (v1)

Design (agreed with the user 2026-11-01): Chrome on Android talks to the Clip directly over USB (WebUSB) from a
web page; no USB networking (Android 17 does not take the Clip's CDC-ECM link as Ethernet) and no app. The same
interface is driven from the laptop by `tools/phone/usbclient.py` for testing.

## Gadget

- The existing configfs gadget (`/sys/kernel/config/usb_gadget/magpie`, `0525:a4a1`, config `c.1` with
  `ecm.usb0`) stays as it is; a FunctionFS function `ffs.magpie` is added to the same configuration, so the
  laptop keeps its network link and the phone gets an extra interface.
- FunctionFS comes from `usb_f_fs.ko` (the kernel is built with `CONFIG_USB_FUNCTIONFS=m`, which builds
  `CONFIG_USB_F_FS=m`; nothing built-in changes). Mount: `mount -t functionfs magpie /dev/ffs-magpie`.
- Start order (init script, after S03usbnet): insmod, `mkdir functions/ffs.magpie`, mount, start
  `magpie-usbd`, wait until it has written its descriptors (`/dev/ffs-magpie/ep1` exists), then unbind the
  UDC, link `ffs.magpie` into `c.1`, rebind. If any step fails the ECM-only gadget is left bound (the laptop
  link must never depend on the daemon). The ECM function stays first in the configuration.
- `magpie-usbd` must survive the host going away (unplug, sleep/wake with the gadget unbound and rebound by
  magpie-lifelogd): FunctionFS events UNBIND/DISABLE/SUSPEND mean "stop I/O and wait"; BIND/ENABLE mean
  "serve again". Endpoint I/O errors (`ESHUTDOWN`, `EIO`, …) end the current request, never the daemon.

## Interface descriptors

One interface, class/subclass/protocol `0xFF/0x00/0x00` (vendor specific, which Chrome's WebUSB may claim),
interface string `"Magpie data"`, two bulk endpoints:

| endpoint | direction | full speed | high speed |
|---|---|---|---|
| ep1 | OUT (host → Clip): requests | 64 | 512 |
| ep2 | IN (Clip → host): responses | 64 | 512 |

Descriptors are written in the FunctionFS v2 format (`FUNCTIONFS_DESCRIPTORS_MAGIC_V2`, FS + HS counts,
`FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC`), followed by the strings block (language 0x0409).
No MS OS descriptors and no WebUSB BOS/landing page (Chrome on Android does not need them).

## Framing

All integers little-endian. Every request is one header followed by `len` payload bytes; every response is one
header followed by `len` payload bytes. A transfer may be split across USB packets; the receiver reassembles by
`len`. The host sends one request and reads its complete response before sending the next (no pipelining).

```
struct hdr {            // 16 bytes
    uint32_t magic;     // 0x3150474D = "MGP1" (bytes 'M','G','P','1')
    uint16_t cmd;       // request: command; response: same command | 0x8000
    uint16_t flags;     // 0 (reserved)
    uint32_t seq;       // chosen by the host, echoed in the response
    uint32_t len;       // payload bytes that follow
};
```

Response payloads start with a 4-byte signed status (`int32`, 0 = OK, negative = `-errno`), then the result.
Requests with a bad magic, unknown cmd, or `len` > 65536 get status `-EINVAL` (`-22`) and an empty result;
the daemon then discards input until the next packet boundary (it must never wedge).

Text payloads (requests and JSON results) are UTF-8 JSON objects, no trailing NUL.

Clarifications (from the first client implementation):
- The device never sends a zero-length packet to end a response; the host frames by `len` and sizes its reads
  to whole packets. The host likewise never sends a ZLP after a request.
- An error reply to a request whose header could be parsed echoes that request's `cmd | 0x8000` and `seq`;
  for an unparsable header it uses `cmd = 0x8000`, `seq = 0`.
- LIST `mtime` is seconds since the epoch (UTC). HASH `size` is the file size in bytes. DELETE `err` is a short
  free-text reason (clients only rely on `ok`).
- READ with `offset` at or past the end of the file returns status 0 and 0 bytes (EOF), not an error.
- A client that dies mid-exchange leaves its response queued in the device. Every client must therefore, after
  claiming the interface, read and discard IN data until a short timeout, and must skip responses whose `seq`
  is earlier than the one it is waiting for (seq numbers start at a random value per session in the page).

## Commands

All file names are bare names inside `/data/photos` matching `^[0-9]{8}-[0-9]{6}\.(jpg|snap|snap\.time)$`;
anything else (paths, `..`, other names) gets `-EINVAL`. Nothing outside `/data/photos` is ever read or
deleted, except the clock files written by SETTIME.

| cmd | name | request payload | result (after the status) |
|---|---|---|---|
| 0x0001 | STATUS | empty | JSON: `{"proto":1,"version":"…","clip_utc_ms":N,"uptime_s":F,"rtt":N\|null,"photos":N,"snaps":N,"snap_times":N,"bytes":N,"free_kb":N,"battery_raw":N\|null,"charging":true\|false\|null}` |
| 0x0002 | LIST | JSON `{"after":"<name>"\|"","max":N}` (max ≤ 500, default 200) | JSON `{"files":[{"name":…,"size":N,"mtime":N},…],"more":true\|false}`, names in ascending order, strictly after `after` |
| 0x0003 | READ | JSON `{"name":…,"offset":N,"len":N}` (len ≤ 65536 − 4) | raw file bytes (fewer at EOF; 0 bytes = EOF) |
| 0x0004 | HASH | JSON `{"name":…}` | JSON `{"name":…,"size":N,"sha256":"<64 hex>"}` |
| 0x0005 | DELETE | JSON `{"files":[{"name":…,"sha256":"<hex>"},…]}` (≤ 500) | JSON `{"results":[{"name":…,"ok":true\|false,"err":"…"},…]}`: a file is deleted only if its current sha256 matches |
| 0x0006 | TIME | empty | JSON `{"clip_utc_ms":N,"mono_ns":N,"rtt":N\|null}` (sampled when the request is processed) |
| 0x0007 | SETTIME | JSON `{"utc_ms":N}` (2020-01-01 ≤ utc_ms < 2100-01-01, else -EINVAL) | JSON `{"before_ms":N,"after_ms":N,"rtc":"ok"\|"err","rtt_cal":"…"\|null}` |

- STATUS `version`: daemon version string. `rtt`: the RTT counter (see below) or null if unreadable.
  `battery_raw` from `magpie-battery` (`raw:` line), `charging` from `magpie-charge`; null if the tool fails.
  `bytes`: total size of the listed files.
- READ: the host reads a file in chunks; the page verifies the result against HASH before offering delete.
- SETTIME: the host measures the round trip with a few TIME requests and sends `utc_ms` = its clock at the
  moment the request will be processed (send time + half the shortest round trip). The daemon then:
  1. sets the system clock (`clock_settime(CLOCK_REALTIME)`), reports `before_ms` (the Clip clock just before)
     and `after_ms`;
  2. writes the RTC like `hwclock -w -u` (run that command; `rtc` = `ok`/`err`);
  3. if the RTT runs at the magpie prescaler (RTT_MR & 0xFFFF == 32, see `tools/magpie_timesync.py`) and
     `/data/time/rtt_epoch` exists, appends one calibration line to `/data/time/rtt_cal` in the same format as
     `magpie_timesync.py` (`cal=<ISO UTC>Z epoch_fit=<epoch> diff_ms=<ms> fit_roundtrip_ms=<ms>`), using the
     RTT count read right after setting the clock and `fit_roundtrip_ms` = 0 (the phone's own estimate is
     unknown to the daemon); `rtt_cal` = that line, else null. It never restarts or rewrites the RTT.
     Formula: `epoch_fit = t_after − count × pres / 32768` (seconds, `t_after` = system clock right after the
     set, `pres` = RTT_MR & 0xFFFF), `diff_ms = (epoch_fit − epoch_utc) × 1000` with `epoch_utc` from the last
     line of `/data/time/rtt_epoch` (`epoch_utc=<s> pres=32 set=<ISO>Z …`); printed as
     `cal=%Y-%m-%dT%H:%M:%SZ epoch_fit=%.4f diff_ms=%+.1f fit_roundtrip_ms=%.1f`.
- RTT registers (SAM9X5 RTT, base 0xFFFFFE20): `RTT_MR` +0x00, `RTT_VR` +0x08; read `RTT_VR` twice until two
  reads agree (the datasheet's rule for an asynchronous counter). Map them through `/dev/mem` (as gpsnap does).

## Limits and behaviour

- One host at a time; requests are served in order. Large READs are the bulk of the traffic: aim for
  ≥ 1 MB/s at high speed (64 KiB chunks).
- The daemon runs as root (it deletes files and sets the clock), opens nothing but `/data/photos/<valid name>`,
  the clock devices and the tools above, and logs one line per DELETE and SETTIME to the kernel log
  (`/dev/kmsg`, prefix `magpie-usbd:`), like the init scripts do.
- Version 1 has no authentication: anyone with physical USB access to the Clip already has telnet/FTP over the
  ECM link today.
