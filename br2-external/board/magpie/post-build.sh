#!/bin/sh
# Buildroot post-build (runs after the overlay): drop BusyBox's stock init scripts we don't use.
# S50telnet would start a second, login-protected telnetd on port 23 and clash with S20services.
set -eu
TARGET_DIR="$1"
# Also old names of our own scripts (an incremental build keeps them in target/), and
# S40network: lo is set up by S03usbnet, so ifup -a only fails with "File exists".
for s in S50telnet S01seedrng S01syslogd S02klogd S50crond S90httpd S41ifplugd \
         S03logs S10usbnet S20services S99healthy \
         S40network; do
    rm -f "$TARGET_DIR/etc/init.d/$s"
done
# The external toolchain also ships C++, Fortran and OpenMP runtimes; everything on the device is C.
rm -f "$TARGET_DIR"/usr/lib/libstdc++.so* "$TARGET_DIR"/usr/lib/libgfortran.so* "$TARGET_DIR"/usr/lib/libgomp.so*
