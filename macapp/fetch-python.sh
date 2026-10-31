#!/usr/bin/env bash
# Puts a private copy of Python inside diskmap.app so it runs on Macs without Python or developer tools.
#
#   bash macapp/fetch-python.sh <arm64|x86_64> <destination folder> [python to precompile with]
#
# Uses python-build-standalone (the relocatable CPython builds that uv and others ship), pinned to one
# release and checked against its SHA-256. Downloads are cached in ~/Library/Caches/diskmap-build.
set -euo pipefail

PBS_TAG="20261009"
PY_VERSION="3.12.15"
SHA_arm64="233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f"
SHA_x86_64="fb3187d95334813b88940db0f1ca57b84cedfd552739c3a5771c3ea0e49988c9"

ARCH="${1:-}"
DEST="${2:-}"
COMPILER="${3:-}"
case "$ARCH" in
  arm64)  TRIPLE="aarch64-apple-darwin"; SHA="$SHA_arm64" ;;
  x86_64) TRIPLE="x86_64-apple-darwin";  SHA="$SHA_x86_64" ;;
  *) echo "usage: fetch-python.sh <arm64|x86_64> <destination> [python-for-precompiling]" >&2; exit 2 ;;
esac
[ -n "$DEST" ] || { echo "fetch-python.sh: missing destination folder" >&2; exit 2; }

FILE="cpython-${PY_VERSION}+${PBS_TAG}-${TRIPLE}-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${FILE}"
CACHE="${DISKMAP_BUILD_CACHE:-$HOME/Library/Caches/diskmap-build}"
mkdir -p "$CACHE"

sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }

if [ ! -f "$CACHE/$FILE" ] || [ "$(sha256 "$CACHE/$FILE")" != "$SHA" ]; then
  echo "  downloading Python ${PY_VERSION} for ${ARCH}…"
  rm -f "$CACHE/$FILE.part"
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 --progress-bar -o "$CACHE/$FILE.part" "$URL"
  mv "$CACHE/$FILE.part" "$CACHE/$FILE"
fi
got="$(sha256 "$CACHE/$FILE")"
if [ "$got" != "$SHA" ]; then
  rm -f "$CACHE/$FILE"
  echo "Python download didn't match its checksum (got $got). Try again; if it keeps happening, the download is being altered." >&2
  exit 1
fi

rm -rf "$DEST/python"
mkdir -p "$DEST"
tar -xzf "$CACHE/$FILE" -C "$DEST"
PYDIR="$DEST/python"
LIB="$PYDIR/lib/python3.12"
[ -x "$PYDIR/bin/python3.12" ] || { echo "Unexpected Python archive layout." >&2; exit 1; }

# Trim what diskmap never uses (GUI toolkit, IDE, installers, headers): ~67 MB -> ~35 MB.
rm -rf "$PYDIR/include" "$PYDIR/share" "$PYDIR/lib/pkgconfig"
rm -rf "$PYDIR"/lib/itcl* "$PYDIR"/lib/tcl* "$PYDIR"/lib/tk* "$PYDIR"/lib/thread* "$PYDIR"/lib/libtcl* "$PYDIR"/lib/libtk*
rm -rf "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" "$LIB/ensurepip" "$LIB/lib2to3" "$LIB/pydoc_data" "$LIB/test" "$LIB/venv"
rm -f  "$LIB"/lib-dynload/_tkinter* "$LIB/turtle.py"
rm -rf "$LIB/site-packages"/* 2>/dev/null || true
find "$PYDIR/bin" -mindepth 1 ! -name python3 ! -name python3.12 -exec rm -rf {} +
find "$PYDIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

# Precompile the standard library once, so the app starts fast and never writes inside itself.
# Bytecode doesn't depend on the CPU, so the arm64 Python can compile the Intel copy too.
PYC="$COMPILER"
if [ -z "$PYC" ] && "$PYDIR/bin/python3" -c "pass" >/dev/null 2>&1; then PYC="$PYDIR/bin/python3"; fi
if [ -n "$PYC" ] && "$PYC" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
  # (no -x filter: the unused packages are already deleted, and a filter would also match the folder path)
  "$PYC" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$LIB" >/dev/null ||
    echo "  (some files didn't precompile; harmless, they compile in memory when used)"
else
  echo "  (skipping precompile: no Python 3.12 that runs here; the app will still work, just start a little slower)"
fi

# Prove it works when we can run it.
if "$PYDIR/bin/python3" -c "pass" >/dev/null 2>&1; then
  "$PYDIR/bin/python3" -B -s -c "import sqlite3, ssl, ctypes, json, plistlib, http.server, urllib.request, socket; print('  python', __import__('sys').version.split()[0], 'ok')"
fi
echo "  bundled Python: $(du -sh "$PYDIR" | cut -f1)"
