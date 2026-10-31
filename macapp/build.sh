#!/usr/bin/env bash
# Builds diskmap.app from this folder and installs it to ~/Applications.
#   bash ~/projects/diskmap/macapp/build.sh
# Re-run it whenever the dashboard code changes. Needs Xcode or the Command Line Tools.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD="$HERE/build"
APP="$BUILD/diskmap.app"
DEST="$HOME/Applications"

command -v swiftc >/dev/null || { echo "swiftc not found. Install the Command Line Tools: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found. Install the Command Line Tools: xcode-select --install"; exit 1; }

echo "→ Compiling the app"
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/diskmap"
swiftc -O -swift-version 5 -parse-as-library \
  -target "$(uname -m)-apple-macos12.0" \
  -framework Cocoa -framework WebKit \
  -o "$APP/Contents/MacOS/diskmap" "$HERE/App.swift"

echo "→ Bundling the dashboard"
for f in server.py diskmap.py macstats.py history.py inventory.py template.html home.html city.html widget.html; do
  cp "$ROOT/$f" "$APP/Contents/Resources/diskmap/"
done
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"

echo "→ Making the icon"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "→ Signing (ad-hoc, for this Mac only)"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "  (codesign skipped)"

echo "→ Installing to ~/Applications"
mkdir -p "$DEST"
if pgrep -x diskmap >/dev/null; then
  osascript -e 'tell application id "local.diskmap.app" to quit' >/dev/null 2>&1 || pkill -x diskmap || true
  sleep 1
fi
rm -rf "$DEST/diskmap.app"
cp -R "$APP" "$DEST/"
# make sure Launchpad / Spotlight pick up the new icon
touch "$DEST/diskmap.app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/diskmap.app" >/dev/null 2>&1 || true

echo "✓ Installed ~/Applications/diskmap.app"
open "$DEST/diskmap.app"
