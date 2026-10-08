#!/bin/sh
# One-command install of the Switchboard Mini worker.
#
#   sh mini/install.sh                  # links into ~/.local/bin
#   sh mini/install.sh --prefix /usr/local   # needs sudo for that directory
#   sh mini/install.sh --uninstall
#
# Nothing is downloaded and nothing is compiled: the worker is standard-library Python,
# so this only links the launcher onto PATH. Re-running it is safe.
set -eu

PREFIX="$HOME/.local"
ACTION=install
for arg in "$@"; do
  case "$arg" in
    --prefix=*) PREFIX="${arg#--prefix=}" ;;
    --prefix) shift ;;
    --uninstall) ACTION=uninstall ;;
    -h|--help)
      echo "usage: sh mini/install.sh [--prefix DIR] [--uninstall]"
      exit 0 ;;
    *) ;;
  esac
done
if [ "${1:-}" = "--prefix" ] && [ -n "${2:-}" ]; then
  PREFIX="$2"
fi

HERE=$(cd -P "$(dirname "$0")" && pwd)
LAUNCHER="$HERE/bin/switchboard-mini"
TARGET_DIR="$PREFIX/bin"
TARGET="$TARGET_DIR/switchboard-mini"

if [ ! -f "$LAUNCHER" ]; then
  echo "install: $LAUNCHER is missing; run this from a checkout of the repository" >&2
  exit 2
fi

if [ "$ACTION" = uninstall ]; then
  rm -f "$TARGET"
  echo "removed $TARGET"
  exit 0
fi

PY=$(command -v python3 2>/dev/null || true)
if [ -z "$PY" ] && [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; fi
if [ -z "$PY" ]; then
  echo "install: no python3 on PATH. Install the Command Line Tools (xcode-select --install)." >&2
  exit 2
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || {
  echo "install: python3 must be 3.9 or newer (found $($PY -V 2>&1))." >&2
  exit 2
}

mkdir -p "$TARGET_DIR"
chmod +x "$LAUNCHER"
ln -sf "$LAUNCHER" "$TARGET"

echo "installed: $TARGET -> $LAUNCHER"
case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *) echo "note: $TARGET_DIR is not on your PATH. Add it, or run the launcher by full path." ;;
esac
echo
echo "next:  switchboard-mini probe          # read-only capability probe (real Mail.app)"
echo "       switchboard-mini probe --out probe-rows.jsonl"
echo "       switchboard-mini --fixture-mode probe   # labelled FIXTURE: rows, no Mail needed"
