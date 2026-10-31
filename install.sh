#!/usr/bin/env bash
# diskmap installer. Paste into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/muhamrif/diskmap/main/install.sh | bash
#
# It downloads the ready-made app for your Mac (Apple silicon or Intel) from GitHub Releases, checks it,
# and puts it in your Applications folder. Nothing else is installed: Python is built into the app.
# Run the same command again to update. Your history and settings are kept.
#
# Options (add after "bash -s --"):   --uninstall   --purge (uninstall + delete history/settings)
#                                     --version 1.2.0   --from-zip <file>   --source (build from source)
#
# Written for the bash 3.2 that ships with macOS. Everything runs inside main(), so a half-downloaded
# script does nothing.

REPO="muhamrif/diskmap"
APP_NAME="diskmap.app"
BUNDLE_ID="local.diskmap.app"
SYSTEM_APPS="${DISKMAP_TEST_SYSTEM_APPS:-/Applications}"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\033[1m→\033[0m %s\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; [ -n "${2:-}" ] && printf '%s\n' "$2" >&2; exit 1; }

cleanup() { [ -n "${TMP:-}" ] && [ -d "$TMP" ] && rm -rf "$TMP"; }

mac_arch() {
  # hw.optional.arm64 is 1 on Apple silicon even when Terminal runs under Rosetta (where uname -m says x86_64).
  if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then echo arm64; else echo x86_64; fi
}

dev_tools_installed() {
  local d
  d="$(xcode-select -p 2>/dev/null)" || return 1
  [ -n "$d" ] && [ -d "$d" ]
}

running() { pgrep -x diskmap >/dev/null 2>&1; }

quit_running_app() {
  running || return 0
  step "Closing the running diskmap"
  osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
  local i=0
  while running && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
  if running; then pkill -x diskmap 2>/dev/null || true; sleep 1; fi
  if running; then pkill -9 -x diskmap 2>/dev/null || true; sleep 1; fi
}

pick_destination() {
  # Prefer ~/Applications (no admin needed); fall back to /Applications if that's where we can write.
  local home_apps="$HOME/Applications"
  if [ -d "$home_apps/$APP_NAME" ] && [ -w "$home_apps" ]; then echo "$home_apps"; return; fi
  if [ -d "$SYSTEM_APPS/$APP_NAME" ] && [ -w "$SYSTEM_APPS" ] && [ -w "$SYSTEM_APPS/$APP_NAME" ]; then echo "$SYSTEM_APPS"; return; fi
  mkdir -p "$home_apps" 2>/dev/null || true
  if [ -d "$home_apps" ] && [ -w "$home_apps" ]; then echo "$home_apps"; return; fi
  if [ -w "$SYSTEM_APPS" ]; then echo "$SYSTEM_APPS"; return; fi
  echo ""
}

download() {   # url, output file
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 600 -sS -o "$2" "$1"
}

install_app_from_dir() {   # folder that contains diskmap.app
  local src="$1/$APP_NAME" dest py
  [ -d "$src/Contents/MacOS" ] || fail "The download doesn't contain diskmap.app." "Please try again in a minute."

  py="$src/Contents/Resources/python/bin/python3"
  if [ -x "$py" ]; then
    step "Checking it runs on this Mac"
    "$py" -B -s -c "import sqlite3, ssl, ctypes, json" >/dev/null 2>&1 ||
      fail "The downloaded app doesn't run on this Mac." "Your Mac: $(mac_arch), macOS $(sw_vers -productVersion). Please send this message to whoever shared diskmap with you."
  fi

  dest="$(pick_destination)"
  [ -n "$dest" ] || fail "Couldn't find a folder to install into." "Neither ~/Applications nor /Applications can be written by your account."

  quit_running_app
  step "Installing to ${dest/#$HOME/~}"
  rm -rf "$dest/.$APP_NAME.new" 2>/dev/null
  ditto "$src" "$dest/.$APP_NAME.new" || fail "Couldn't copy diskmap into ${dest/#$HOME/~}." "Is the disk full?"
  xattr -dr com.apple.quarantine "$dest/.$APP_NAME.new" 2>/dev/null || true
  if [ -e "$dest/$APP_NAME" ]; then
    if ! mv "$dest/$APP_NAME" "$dest/.$APP_NAME.old" 2>/dev/null; then
      rm -rf "$dest/.$APP_NAME.new"
      fail "macOS didn't let Terminal replace the old diskmap." \
"Open System Settings › Privacy & Security › App Management, turn on Terminal, then run the install command again.
Or drag the old diskmap from ${dest/#$HOME/~} to the Trash first."
    fi
  fi
  mv "$dest/.$APP_NAME.new" "$dest/$APP_NAME" || { mv "$dest/.$APP_NAME.old" "$dest/$APP_NAME" 2>/dev/null; fail "Couldn't finish installing."; }
  rm -rf "$dest/.$APP_NAME.old" 2>/dev/null || true

  # An older copy somewhere else would confuse Spotlight and Open at Login.
  local other
  for other in "$SYSTEM_APPS/$APP_NAME" "$HOME/Applications/$APP_NAME"; do
    [ "$other" = "$dest/$APP_NAME" ] && continue
    if [ -d "$other" ] && [ "$(defaults read "$other/Contents/Info" CFBundleIdentifier 2>/dev/null)" = "$BUNDLE_ID" ]; then
      if mv "$other" "$HOME/.Trash/diskmap (old copy $(date +%s)).app" 2>/dev/null; then
        warn "Moved an older copy of diskmap from ${other%/*} to the Trash."
      else
        warn "There's another copy of diskmap in ${other%/*}. Drag it to the Trash so you don't open the old one."
      fi
    fi
  done

  [ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$dest/$APP_NAME" >/dev/null 2>&1 || true
  INSTALLED="$dest/$APP_NAME"
}

build_from_source() {
  dev_tools_installed || fail "Building from source needs Apple's Command Line Tools." \
"Run  xcode-select --install , wait for it to finish, then run this command again with --source."
  local src="$HOME/.diskmap/src"
  if [ -d "$src/.git" ]; then
    step "Updating the source"
    git -C "$src" pull --ff-only --quiet || fail "Couldn't update $src." "Delete that folder and try again."
  else
    step "Downloading the source"
    rm -rf "$src"; mkdir -p "$HOME/.diskmap"
    git clone --depth 1 --quiet "https://github.com/$REPO.git" "$src" || fail "Couldn't download the source from GitHub."
  fi
  quit_running_app
  bash "$src/macapp/build.sh" --no-open || fail "The build failed." "Scroll up for the error."
  INSTALLED="$HOME/Applications/$APP_NAME"
}

uninstall() {
  local purge="$1" p
  quit_running_app
  for p in "$HOME/Applications/$APP_NAME" "$SYSTEM_APPS/$APP_NAME"; do
    if [ -d "$p" ] && [ "$(defaults read "$p/Contents/Info" CFBundleIdentifier 2>/dev/null)" = "$BUNDLE_ID" ]; then
      rm -rf "$p" && step "Removed ${p/#$HOME/~}"
    fi
  done
  launchctl unload "$HOME/Library/LaunchAgents/$BUNDLE_ID.plist" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/$BUNDLE_ID.plist"
  rm -rf "$HOME/.diskmap"
  if [ "$purge" = 1 ]; then
    rm -rf "$HOME/Library/Application Support/diskmap" "$HOME/Library/Caches/diskmap-build"
    defaults delete "$BUNDLE_ID" >/dev/null 2>&1 || true
    step "Deleted diskmap's history and settings"
  else
    echo "Your history and settings are still in ~/Library/Application Support/diskmap (use --purge to delete them)."
  fi
  bold "✓ diskmap is uninstalled."
}

main() {
  set -u
  # Use Apple's own tools even if Homebrew's versions come first in PATH. (Tests put fakes in front.)
  PATH="${DISKMAP_TEST_PATH:+$DISKMAP_TEST_PATH:}/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
  export PATH
  local mode="install" version="" from_zip="" purge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --uninstall) mode="uninstall" ;;
      --purge) mode="uninstall"; purge=1 ;;
      --source) mode="source" ;;
      --version) version="${2:-}"; shift ;;
      --from-zip) from_zip="${2:-}"; shift ;;
      -h|--help) sed -n '2,15p' "$0" 2>/dev/null || echo "See https://github.com/$REPO"; exit 0 ;;
      *) fail "Unknown option: $1" ;;
    esac
    shift
  done

  [ "$(uname -s)" = "Darwin" ] || fail "diskmap only runs on a Mac."
  local osv major
  osv="$(sw_vers -productVersion 2>/dev/null || echo 0)"
  major="${osv%%.*}"
  [ "$major" -ge 12 ] 2>/dev/null || fail "diskmap needs macOS 12 (Monterey) or newer. This Mac has macOS $osv."
  [ "$(id -u)" != "0" ] || fail "Please run this without sudo." "diskmap installs just for your account."

  if [ "$mode" = "uninstall" ]; then uninstall "$purge"; return; fi

  local arch avail_kb
  arch="$(mac_arch)"
  avail_kb="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "$avail_kb" ] && [ "$avail_kb" -lt 307200 ] 2>/dev/null; then
    fail "There isn't enough free space (diskmap needs about 300 MB while installing)." "Free some space and try again."
  fi

  bold "Installing diskmap for $( [ "$arch" = arm64 ] && echo "Apple silicon" || echo "Intel") · macOS $osv"
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/diskmap.XXXXXX")" || fail "Couldn't create a temporary folder."
  trap cleanup EXIT
  trap 'exit 130' INT TERM

  if [ "$mode" = "source" ]; then
    build_from_source
  else
    local zip="$TMP/diskmap.zip" asset="diskmap-macos-$arch.zip" base
    if [ -n "$from_zip" ]; then
      [ -f "$from_zip" ] || fail "No such file: $from_zip"
      cp "$from_zip" "$zip"
    else
      if [ -n "$version" ]; then base="https://github.com/$REPO/releases/download/v${version#v}"
      else base="https://github.com/$REPO/releases/latest/download"; fi
      step "Downloading diskmap"
      if ! download "$base/$asset" "$zip"; then
        if ! curl -fsS --connect-timeout 10 -o /dev/null "https://github.com" 2>/dev/null; then
          fail "Couldn't reach GitHub." "Check your internet connection (or VPN / firewall) and try again."
        fi
        if dev_tools_installed; then
          warn "No ready-made download found; building from source instead."
          build_from_source
        else
          fail "There's no ready-made diskmap download yet${version:+ for version $version}." \
"Ask whoever shared diskmap to publish a release, or check https://github.com/$REPO/releases"
        fi
      fi
      if [ -z "${INSTALLED:-}" ]; then
        step "Verifying the download"
        if download "$base/SHA256SUMS.txt" "$TMP/SHA256SUMS.txt"; then
          local want got
          want="$(awk -v f="$asset" '$2 == f {print $1}' "$TMP/SHA256SUMS.txt")"
          got="$(shasum -a 256 "$zip" | awk '{print $1}')"
          [ -n "$want" ] || fail "The release doesn't list a checksum for $asset."
          [ "$want" = "$got" ] || fail "The download is damaged or was changed in transit." "Please run the command again."
        else
          fail "Couldn't download the checksum file to verify diskmap." "Please run the command again."
        fi
      fi
    fi
    if [ -z "${INSTALLED:-}" ]; then
      step "Unpacking"
      mkdir -p "$TMP/x"
      ditto -x -k "$zip" "$TMP/x" || fail "The download couldn't be unpacked." "Please run the command again."
      install_app_from_dir "$TMP/x"
    fi
  fi

  bold "✓ diskmap $(defaults read "$INSTALLED/Contents/Info" CFBundleShortVersionString 2>/dev/null) is installed in ${INSTALLED%/*}"
  open "$INSTALLED" 2>/dev/null || warn "Open diskmap from your Applications folder."
  cat <<'MSG'

First time? A few one-time steps (System Settings › Privacy & Security):
  • Full Disk Access → turn on diskmap (it's in the list; if not, click + and pick it
    from Applications). Then quit diskmap from its menu bar icon and open it again.
  • When diskmap asks to control Safari, Chrome, Finder or System Events, click Allow.
  • Allow its notifications when asked.
diskmap's Diagnostics tab shows anything still missing, with buttons that fix it.

To update later, run the same install command again.
MSG
}

main "$@"
