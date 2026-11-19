#!/bin/sh
# Capture a kernel event trace of a command on the QNX target and turn it into
# a Perfetto timeline on the host.
#
#   tools/trace-run.sh NAME SECONDS 'COMMAND ON TARGET'
#
# Steps: start `tracelogger -w -s SECONDS` on the target, run COMMAND, copy
# NAME.kev back into captures/, print it with the SDP's host `traceprinter`
# (inside the build container), then run kevscope on the text.
# FAST=1 captures fast-mode events instead of wide ones.
set -e
[ $# -eq 3 ] || { sed -n '2,10p' "$0"; exit 2; }
NAME="$1"; SECS="$2"; CMD="$3"
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
WIDE="-w"; [ -n "$FAST" ] && WIDE=""
mkdir -p captures
echo "== tracing on the target for $SECS s"
tools/target.sh run "rm -f /dev/shmem/$NAME.kev; (tracelogger $WIDE -s $SECS -f /dev/shmem/$NAME.kev >/dev/null 2>&1 &); sleep 1; $CMD; sleep $((SECS + 1)); ls -l /dev/shmem/$NAME.kev"
tools/target.sh get "/dev/shmem/$NAME.kev" "captures/$NAME.kev"
echo "== traceprinter (QNX SDP host tool, in the container)"
docker/sdp.sh sh bash -c ". /root/qnx800/qnxsdp-env.sh >/dev/null; traceprinter -f captures/$NAME.kev -o captures/$NAME.txt"
echo "== kevscope"
python3 -m kevscope convert "captures/$NAME.txt" -o "captures/$NAME.json"
python3 -m kevscope stats "captures/$NAME.txt" --top 15
