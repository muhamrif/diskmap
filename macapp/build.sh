#!/usr/bin/env bash
# Builds diskmap.app.
#
#   bash macapp/build.sh                  build for this Mac (Python built in) and install to ~/Applications
#   bash macapp/build.sh --release        build both Apple silicon and Intel zips into dist/ (what GitHub Releases ship)
#
# Options:
#   --arch arm64|x86_64    CPU to build for (default: this Mac)
#   --system-python        don't bundle Python; use the Mac's own python3 at runtime (developer builds only)
#   --version X.Y.Z        version to stamp into the app (default: the VERSION file)
#   --no-install           leave the app in macapp/build instead of installing it
#   --no-open              don't open the app after installing
#   --zip                  also write dist/diskmap-macos-<arch>.zip
#
# Needs Apple's Command Line Tools (for the Swift compiler): xcode-select --install
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FILES="server.py diskmap.py macstats.py history.py inventory.py template.html home.html city.html widget.html VERSION"

host_arch() { if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then echo arm64; else echo x86_64; fi; }
ARCH="$(host_arch)"; BUNDLE_PY=1; INSTALL=1; OPEN=1; ZIP=0; RELEASE=0
VERSION="$(tr -d ' \n' < "$ROOT/VERSION" 2>/dev/null || echo 0.0.0)"
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH="${2:-}"; shift ;;
    --system-python) BUNDLE_PY=0 ;;
    --version) VERSION="${2:-}"; shift ;;
    --no-install) INSTALL=0 ;;
    --no-open) OPEN=0 ;;
    --zip) ZIP=1 ;;
    --release) RELEASE=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
VERSION="${VERSION#v}"
case "$ARCH" in arm64|x86_64) ;; *) echo "--arch must be arm64 or x86_64" >&2; exit 2 ;; esac

if [ "$RELEASE" = 1 ]; then
  rm -rf "$ROOT/dist"; mkdir -p "$ROOT/dist"
  bash "$0" --arch arm64  --version "$VERSION" --no-install --zip
  bash "$0" --arch x86_64 --version "$VERSION" --no-install --zip
  ( cd "$ROOT/dist" && shasum -a 256 diskmap-macos-*.zip > SHA256SUMS.txt && cat SHA256SUMS.txt )
  echo "✓ Release files for diskmap $VERSION are in dist/"
  exit 0
fi

[ "$(uname -s)" = "Darwin" ] || { echo "diskmap.app can only be built on a Mac." >&2; exit 1; }
# /usr/bin/swiftc exists even without the developer tools (it just pops up an installer), so ask xcode-select.
devdir="$(xcode-select -p 2>/dev/null || true)"
if [ -z "$devdir" ] || [ ! -d "$devdir" ] || ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "Building diskmap needs Apple's free Command Line Tools."
  echo "Run:  xcode-select --install   then run this script again."
  echo "(Just want the app? Use the one-line installer in the README; it needs nothing.)"
  exit 1
fi

BUILD="$HERE/build/$ARCH"
APP="$BUILD/diskmap.app"
RES="$APP/Contents/Resources"
echo "→ diskmap $VERSION for $ARCH"
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$RES/diskmap"

echo "→ Compiling the app"
xcrun swiftc -O -swift-version 5 -parse-as-library \
  -target "$ARCH-apple-macos12.0" \
  -framework Cocoa -framework WebKit -framework UserNotifications \
  -o "$APP/Contents/MacOS/diskmap" "$HERE/App.swift"

echo "→ Bundling the dashboard"
for f in $FILES; do
  if [ "$f" = VERSION ]; then printf '%s\n' "$VERSION" > "$RES/diskmap/VERSION"; continue; fi
  cp "$ROOT/$f" "$RES/diskmap/"
done
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist"

if [ "$BUNDLE_PY" = 1 ]; then
  echo "→ Adding Python"
  COMPILER=""
  # When building the Intel copy on Apple silicon, precompile with the Apple-silicon Python (same version).
  if [ "$ARCH" != "$(host_arch)" ] && [ -x "$HERE/build/$(host_arch)/diskmap.app/Contents/Resources/python/bin/python3" ]; then
    COMPILER="$HERE/build/$(host_arch)/diskmap.app/Contents/Resources/python/bin/python3"
  fi
  bash "$HERE/fetch-python.sh" "$ARCH" "$RES" "$COMPILER"
  PY="$RES/python/bin/python3"; [ -n "$COMPILER" ] && PY="$COMPILER"
  if "$PY" -c pass >/dev/null 2>&1; then
    "$PY" -m compileall -q --invalidation-mode unchecked-hash "$RES/diskmap" >/dev/null
  fi
else
  echo "→ Using the Mac's own Python at runtime (developer build)"
fi

echo "→ Making the icon"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"
rm -rf "$ICONSET"

echo "→ Signing (ad-hoc, no Apple account needed)"
xattr -cr "$APP" 2>/dev/null || true
# Sign every piece of code inside first (inside-out), then the app itself.
if [ -d "$RES/python" ]; then
  find "$RES/python" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0 |
    while IFS= read -r -d '' f; do
      if file -b "$f" | grep -q "Mach-O"; then codesign --force --sign - --timestamp=none "$f" >/dev/null 2>&1; fi
    done
fi
codesign --force --sign - --timestamp=none --identifier local.diskmap.app "$APP"
codesign --verify --deep --verbose=1 "$APP"

if [ "$ZIP" = 1 ]; then
  mkdir -p "$ROOT/dist"
  rm -f "$ROOT/dist/diskmap-macos-$ARCH.zip"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/dist/diskmap-macos-$ARCH.zip"
  echo "→ dist/diskmap-macos-$ARCH.zip ($(du -h "$ROOT/dist/diskmap-macos-$ARCH.zip" | cut -f1))"
fi

if [ "$INSTALL" = 1 ]; then
  echo "→ Installing to ~/Applications"
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
  if pgrep -x diskmap >/dev/null 2>&1; then
    osascript -e 'tell application id "local.diskmap.app" to quit' >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -x diskmap >/dev/null 2>&1 || break; sleep 0.5; done
    pkill -x diskmap 2>/dev/null || true
    sleep 1
  fi
  rm -rf "$DEST/diskmap.app.new"
  ditto "$APP" "$DEST/diskmap.app.new"
  rm -rf "$DEST/diskmap.app"
  mv "$DEST/diskmap.app.new" "$DEST/diskmap.app"
  touch "$DEST/diskmap.app"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/diskmap.app" >/dev/null 2>&1 || true
  echo "✓ Installed ~/Applications/diskmap.app ($VERSION)"
  [ "$OPEN" = 1 ] && open "$DEST/diskmap.app"
else
  echo "✓ Built $APP"
fi
