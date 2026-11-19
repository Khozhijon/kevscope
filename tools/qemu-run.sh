#!/bin/sh
# Boot a QNX 8.0 aarch64 image built by mkqnximage on a Mac (or Linux) host.
#
#   tools/qemu-run.sh [TARGET_DIR]
#
# TARGET_DIR is the directory where `mkqnximage --type=qemu --arch=aarch64le`
# was run (default ~/qnx-linux/home/targets/a64). The guest's SSH port is
# forwarded to 127.0.0.1:$SSH_PORT (default 2222); the serial console is this
# terminal (Ctrl-A X quits QEMU).
#
#   ACCEL=tcg  (default)  software emulation, works everywhere
#   ACCEL=hvf             Apple Hypervisor.framework
#   MACHINE, CPU, SMP, RAM, EXTRA   override the QEMU machine settings
set -e
T="${1:-$HOME/qnx-linux/home/targets/a64}"
ACCEL="${ACCEL:-tcg}"
SSH_PORT="${SSH_PORT:-2222}"
SMP="${SMP:-2}"
RAM="${RAM:-1G}"
[ -f "$T/output/ifs.bin" ] || { echo "No image in $T/output (run mkqnximage first)" >&2; exit 1; }
MAC="$(sed -n "s/^OPT_MACADDR='\(.*\)'/\1/p" "$T/local/options")"
if [ "$ACCEL" = hvf ]; then
  MACHINE="${MACHINE:-virt}"; CPU="${CPU:-host}"
else
  # mkqnximage's own runner uses virt-4.2 + cortex-a57; current QEMU has dropped
  # the 4.2 machine, so use the oldest one that is not deprecated.
  MACHINE="${MACHINE:-virt-8.2}"; CPU="${CPU:-cortex-a57}"
fi
set -x
exec qemu-system-aarch64 -accel "$ACCEL" -machine "$MACHINE" -cpu "$CPU" -smp "$SMP" -m "$RAM" \
  -drive file="$T/output/disk-qemu",format=raw,if=none,id=drv0 -device virtio-blk-device,drive=drv0 \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
  -device virtio-net-device,netdev=net0,mac="${MAC:-52:54:00:12:34:56}" \
  -object rng-random,filename=/dev/urandom,id=rng0 -device virtio-rng-device,rng=rng0 \
  -kernel "$T/output/ifs.bin" -display none -serial mon:stdio $EXTRA
