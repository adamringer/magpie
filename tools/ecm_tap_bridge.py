#!/usr/bin/env python3
"""Userspace CDC-ECM <-> TAP bridge for the Narrative Clip 1 in Ethernet mode.

The kernel's cdc_ether driver keeps the link down because the Clip never sends
a valid NETWORK_CONNECTION notification. This script detaches cdc_ether, talks
to the CDC Data bulk endpoints directly with pyusb, and passes Ethernet frames
to and from a TAP interface, so the carrier is never checked.

Run as root (TAP creation, driver detach):
    sudo .venv/bin/python -I -B tools/ecm_tap_bridge.py
Then: ping 192.168.2.2 / telnet 192.168.2.2. Ctrl-C restores cdc_ether.
"""
import argparse
import fcntl
import os
import struct
import subprocess
import sys
import threading

import usb.core
import usb.util

VID, PID = 0x0525, 0xA4A1
COMM_IF, DATA_IF, DATA_ALT = 0, 1, 1
MAX_PACKET = 512

TUNSETIFF = 0x400454CA
IFF_TAP, IFF_NO_PI = 0x0002, 0x1000

# CDC SET_ETHERNET_PACKET_FILTER: directed | broadcast | all multicast
SET_ETH_PACKET_FILTER = 0x43
PACKET_FILTER = 0x0002 | 0x0004 | 0x0008


def find_endpoints(dev):
    """Return (ep_in, ep_out, ep_notify) read from the active configuration.

    The stock firmware uses 0x81/0x02/0x83; a configfs gadget may number them
    differently, so look for the interrupt IN endpoint on the comm interface
    and the bulk endpoints on the data interface's alt setting.
    """
    cfg = dev.get_active_configuration()
    try:
        comm = cfg[(COMM_IF, 0)]
        data = cfg[(DATA_IF, DATA_ALT)]
    except KeyError:
        sys.exit(f"no interface {COMM_IF} alt 0 / interface {DATA_IF} alt {DATA_ALT} on the Clip")
    notify = bulk_in = bulk_out = None
    for ep in comm:
        if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR:
            notify = ep.bEndpointAddress
    for ep in data:
        if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
            continue
        if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
            bulk_in = ep.bEndpointAddress
        else:
            bulk_out = ep.bEndpointAddress
    if None in (notify, bulk_in, bulk_out):
        sys.exit("could not find the ECM interrupt/bulk endpoints in the descriptors")
    return bulk_in, bulk_out, notify


def open_tap(name):
    fd = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI))
    return fd


def ip(*args):
    subprocess.run(["ip", *args], check=True)


def usb_to_tap(dev, tap_fd, stop, ep_in):
    while not stop.is_set():
        try:
            frame = dev.read(ep_in, 16384, timeout=500)
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as e:
            print(f"usb read error: {e}", file=sys.stderr)
            stop.set()
            return
        if len(frame):
            os.write(tap_fd, bytes(frame))


def tap_to_usb(dev, tap_fd, stop, ep_out):
    while not stop.is_set():
        frame = os.read(tap_fd, 2048)
        try:
            dev.write(ep_out, frame, timeout=1000)
            # A frame that fills its last packet needs a ZLP to mark its end.
            if len(frame) % MAX_PACKET == 0:
                dev.write(ep_out, b"", timeout=1000)
        except usb.core.USBError as e:
            print(f"usb write error: {e}", file=sys.stderr)


def notify_logger(dev, stop, ep_notify):
    while not stop.is_set():
        try:
            data = dev.read(ep_notify, 16, timeout=1000)
            print(f"notification: {bytes(data).hex()}", file=sys.stderr)
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError:
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tap", default="magpie0")
    ap.add_argument("--addr", default="192.168.2.10/24")
    ap.add_argument("--mac", default="e6:11:22:33:44:55", help="host-side MAC")
    args = ap.parse_args()

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("Clip (0525:a4a1) not found")

    ep_in, ep_out, ep_notify = find_endpoints(dev)
    print(f"endpoints: in 0x{ep_in:02x} out 0x{ep_out:02x} notify 0x{ep_notify:02x}", flush=True)

    for intf in (COMM_IF, DATA_IF):
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
    usb.util.claim_interface(dev, COMM_IF)
    usb.util.claim_interface(dev, DATA_IF)
    try:
        # Selecting alt 1 on the data interface is what turns the link on.
        dev.set_interface_altsetting(interface=DATA_IF, alternate_setting=DATA_ALT)
        dev.ctrl_transfer(0x21, SET_ETH_PACKET_FILTER, PACKET_FILTER, COMM_IF, None)

        tap_fd = open_tap(args.tap)
        ip("link", "set", args.tap, "address", args.mac)
        ip("addr", "add", args.addr, "dev", args.tap)
        ip("link", "set", args.tap, "up")
        print(f"bridging {args.tap} ({args.addr}) <-> camera; Ctrl-C to stop", flush=True)

        stop = threading.Event()
        threading.Thread(target=usb_to_tap, args=(dev, tap_fd, stop, ep_in), daemon=True).start()
        threading.Thread(target=tap_to_usb, args=(dev, tap_fd, stop, ep_out), daemon=True).start()
        threading.Thread(target=notify_logger, args=(dev, stop, ep_notify), daemon=True).start()
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        usb.util.release_interface(dev, DATA_IF)
        usb.util.release_interface(dev, COMM_IF)
        for intf in (COMM_IF, DATA_IF):
            try:
                dev.attach_kernel_driver(intf)
            except usb.core.USBError:
                pass
        print("released camera, cdc_ether reattached", flush=True)


if __name__ == "__main__":
    main()
