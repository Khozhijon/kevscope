#!/bin/sh
# The whole demo against a running QNX 8.0 target (start it with tools/qemu-run.sh):
# build rtdemo with the QNX toolchain, copy it over, trace it twice (mutex
# without and with priority inheritance) and ask kevscope who kept inv-high waiting.
set -e
cd "$(dirname "$0")/.."
mkdir -p captures
echo "== build with qcc (QNX SDP 8.0, in the container)"
docker/sdp.sh sh bash -c '. /root/qnx800/qnxsdp-env.sh >/dev/null; make -C workload'
tools/target.sh put workload/build/aarch64le/rtdemo /data/home/root/
tools/trace-run.sh inversion 3 '/data/home/root/rtdemo -t 2 -i' > captures/inversion.log
tools/trace-run.sh inherit 3 '/data/home/root/rtdemo -t 2 -i -p' > captures/inherit.log
echo; echo "== mutex without priority inheritance"
python3 -m kevscope blame captures/inversion.txt --thread inv-high --state Mutex-blocked
echo; echo "== mutex with PTHREAD_PRIO_INHERIT"
python3 -m kevscope blame captures/inherit.txt --thread inv-high --state Mutex-blocked
