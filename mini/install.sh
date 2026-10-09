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
# Parse argv positionally, so `--prefix DIR` and `--prefix=DIR` both work and neither
# depends on being the *first* argument. The earlier version consumed the value with a
# bare `shift` inside a `for arg in "$@"` loop (which drops it) and only recovered it
# when `--prefix` happened to be `$1`, so `sh mini/install.sh --uninstall --prefix DIR`
# silently used the default prefix instead. Nothing about the default prefix or
# --uninstall changes here.
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix=*) PREFIX="${1#--prefix=}" ;;
    --prefix)
      if [ "$#" -lt 2 ]; then
        echo "install: --prefix needs a directory, e.g. 'sh mini/install.sh --prefix DIR'" >&2
        exit 2
      fi
      PREFIX="$2"
      shift
      ;;
    --uninstall) ACTION=uninstall ;;
    -h|--help)
      echo "usage: sh mini/install.sh [--prefix DIR] [--prefix=DIR] [--uninstall]"
      exit 0 ;;
    *) ;;
  esac
  shift
done
if [ -z "${PREFIX:-}" ]; then
  echo "install: the prefix is empty; pass a directory (--prefix DIR)" >&2
  exit 2
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
