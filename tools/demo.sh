#!/bin/sh
# The whole demo against a running QNX 8.0 target (start it with tools/qemu-run.sh):
# build rtdemo with the QNX toolchain, copy it over, trace it twice (mutex
# without and with priority inheritance) and ask kevscope who kept inv-high waiting.
# BLAME_OPTS is passed to `kevscope blame` (for example BLAME_OPTS="--shares --top 4").
set -e
cd "$(dirname "$0")/.."
mkdir -p captures
show() { grep -E "^rtdemo: ran|events in," "captures/$1.log" | sed 's/^/   /'; }

echo "== 1. build rtdemo with qcc (QNX SDP 8.0 toolchain, in the container)"
docker/sdp.sh sh bash -c '. /root/qnx800/qnxsdp-env.sh >/dev/null; make -B -C workload' | grep qcc
echo "== 2. copy it to the QNX target"
tools/target.sh put workload/build/aarch64le/rtdemo /data/home/root/
tools/target.sh run 'uname -a'

echo "== 3. trace 'rtdemo -i' on the target (tracelogger -w), print it with traceprinter, convert"
tools/trace-run.sh inversion 3 '/data/home/root/rtdemo -t 2 -i' > captures/inversion.log
show inversion
echo "== 4. same again with 'rtdemo -i -p' (mutex with PTHREAD_PRIO_INHERIT)"
tools/trace-run.sh inherit 3 '/data/home/root/rtdemo -t 2 -i -p' > captures/inherit.log
show inherit

echo
echo "== 5. who ran while inv-high (priority 18) was blocked on the mutex?"
echo "-- mutex without priority inheritance"
python3 -m kevscope blame captures/inversion.txt --thread inv-high --state Mutex-blocked $BLAME_OPTS
echo
echo "-- mutex with PTHREAD_PRIO_INHERIT"
python3 -m kevscope blame captures/inherit.txt --thread inv-high --state Mutex-blocked $BLAME_OPTS
