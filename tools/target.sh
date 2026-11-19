#!/bin/sh
# Talk to the QNX target started by tools/qemu-run.sh.
#   tools/target.sh run  CMD...        run a command on the target
#   tools/target.sh put  FILE... DIR   copy files to the target
#   tools/target.sh get  REMOTE LOCAL  copy a file back
# The image built by mkqnximage has the local test account root / root; change
# with QNX_USER / QNX_PASS. Needs sshpass (brew install sshpass).
set -e
PORT="${SSH_PORT:-2222}"; USER_="${QNX_USER:-root}"; export SSHPASS="${QNX_PASS:-root}"
O="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"
cmd="$1"; shift
# The guest is emulated and can be slow to answer a login; retry copies a few times.
retry() { n=0; until "$@"; do n=$((n + 1)); [ $n -ge 4 ] && return 1; sleep 2; done; }
case "$cmd" in
  run) exec sshpass -e ssh -p "$PORT" $O "$USER_@127.0.0.1" "$@" ;;
  put) eval "dest=\${$#}"; n=$(($# - 1)); files=""; i=0
       for a in "$@"; do i=$((i + 1)); [ $i -le $n ] && files="$files $a"; done
       retry sshpass -e scp -P "$PORT" $O $files "$USER_@127.0.0.1:$dest" ;;
  get) retry sshpass -e scp -P "$PORT" $O "$USER_@127.0.0.1:$1" "$2" ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
