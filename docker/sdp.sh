#!/bin/sh
# Install and drive the Linux build of QNX Software Center inside a linux/amd64
# container on a Mac (QNX ships no macOS host tools for SDP 8.0).
#
#   docker/sdp.sh setup  ~/Downloads/qnx-setup-2.0.4-...-linux.run   install QNX Software Center
#   docker/sdp.sh login                    ask for myQNX e-mail and password (typed by you, hidden)
#   docker/sdp.sh qsc  ARGS...             run qnxsoftwarecenter_clt with your credentials
#   docker/sdp.sh sh   CMD...              run a command in the container (no credentials)
#   docker/sdp.sh logout                   delete the stored credentials
#
# Everything QNX lands outside the repository, under $QNX_HOME (default ~/qnx-linux):
#   $QNX_HOME/home      the container's home (QNX Software Center, ~/.qnx licence files)
#   $QNX_HOME/qnx800    the SDP 8.0 installation
# The container gets a fixed hostname and MAC address so that the licence
# activation sees the same machine every time.
set -e
QNX_HOME="${QNX_HOME:-$HOME/qnx-linux}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=kevscope-qnx-build
AUTH="$QNX_HOME/home/.qnx/kevscope-auth.opts"
QSC=/root/qnxinstall/qnxsoftwarecenter/qnxsoftwarecenter_clt

mkdir -p "$QNX_HOME/home/.qnx" "$QNX_HOME/qnx800"
docker image inspect $IMAGE >/dev/null 2>&1 || \
  docker build --platform linux/amd64 -t $IMAGE "$HERE/docker"

run() {
  docker run --rm --platform linux/amd64 $TTY \
    --hostname kevscope-qnx --mac-address 02:42:ac:11:00:42 \
    -v "$QNX_HOME/home":/root -v "$QNX_HOME/qnx800":/root/qnx800 -v "$HERE":/work \
    -w /work -e HOME=/root $IMAGE "$@"
}
TTY=""; [ -t 0 ] && [ -t 1 ] && TTY="-it"

cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$cmd" in
  setup)
    [ -f "${1:-}" ] || { echo "usage: sdp.sh setup /path/to/qnx-setup-...-linux.run" >&2; exit 2; }
    cp "$1" "$QNX_HOME/home/qnx-setup-lin.run"
    echo "Installing QNX Software Center (this accepts its licence terms: agree-to-license-terms)"
    run sh -c 'chmod a+x /root/qnx-setup-lin.run && /root/qnx-setup-lin.run force-override disable-auto-start agree-to-license-terms /root/qnxinstall'
    run $QSC -version
    ;;
  login)
    prev=""; [ -f "$AUTH" ] && prev="$(sed -n '1s/^-myqnx.user=//p' "$AUTH" | tr -d '"\\')"
    if command -v osascript >/dev/null 2>&1; then
      # macOS: ask in a dialog window, so the password shows as dots while typing.
      user="$(osascript -e 'text returned of (display dialog "myQNX e-mail:" default answer "'"$prev"'" with title "QNX login")')"
      pass="$(osascript -e 'text returned of (display dialog "myQNX password (stays on this Mac):" default answer "" with title "QNX login" with hidden answer)')"
    else
      printf 'myQNX e-mail: '; read -r user
      printf 'myQNX password (hidden, stays on this Mac): '
      stty -echo; read -r pass; stty echo; echo
    fi
    [ -n "$user" ] && [ -n "$pass" ] || { echo "Nothing saved (empty e-mail or password)." >&2; exit 1; }
    umask 077
    printf '%s\n%s\n' "-myqnx.user=$user" "-myqnx.password=$pass" > "$AUTH"
    unset pass
    echo "Saved for this install session in $AUTH (mode 600). Remove with: docker/sdp.sh logout"
    ;;
  logout)
    rm -f "$AUTH" && echo "Credentials removed."
    ;;
  qsc)
    [ -f "$AUTH" ] || { echo "Run docker/sdp.sh login first." >&2; exit 1; }
    run $QSC @/root/.qnx/kevscope-auth.opts "$@"
    ;;
  sh)
    run "$@"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 2
    ;;
esac
