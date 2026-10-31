#!/usr/bin/env bash
# Runs install.sh against simulated Macs (fake curl, sw_vers, sysctl, …) to check every path a friend
# might hit: Apple silicon, Intel, Rosetta, old macOS, offline, bad download, no release yet, updating a
# running copy, low disk space, uninstalling. Works on macOS and Linux.
#
#   bash tests/test_install.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; failn=0

# ---------- fixture: two fake release zips + checksums ----------
make_zip() {   # arch
  local d="$T/fixture/$1"
  mkdir -p "$d/diskmap.app/Contents/MacOS" "$d/diskmap.app/Contents/Resources/python/bin"
  printf '#!/bin/sh\nexit 0\n' > "$d/diskmap.app/Contents/MacOS/diskmap"
  printf '#!/bin/sh\n[ -n "${FAKE_PY_FAIL:-}" ] && exit 1\nexit 0\n' > "$d/diskmap.app/Contents/Resources/python/bin/python3"
  chmod +x "$d/diskmap.app/Contents/MacOS/diskmap" "$d/diskmap.app/Contents/Resources/python/bin/python3"
  printf '<plist><dict><key>CFBundleIdentifier</key><string>local.diskmap.app</string><key>CFBundleShortVersionString</key><string>9.9.%s</string></dict></plist>\n' \
    "$([ "$1" = arm64 ] && echo 1 || echo 2)" > "$d/diskmap.app/Contents/Info.plist"
  (cd "$d" && zip -qry "$T/fixture/diskmap-macos-$1.zip" diskmap.app)
}
make_zip arm64; make_zip x86_64
(cd "$T/fixture" && sha256sum diskmap-macos-*.zip > SHA256SUMS.txt)

# ---------- fake macOS commands ----------
B="$T/bin"; mkdir -p "$B"
fake() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$B/$1"; chmod +x "$B/$1"; }
fake uname     'case "$1" in -s) echo "${FAKE_OS:-Darwin}";; -m) echo "${FAKE_UNAME_M:-arm64}";; *) echo Darwin;; esac'
fake sw_vers   'echo "${FAKE_OSV:-15.1}"'
fake sysctl    'echo "${FAKE_ARM64:-1}"'
fake id        '[ "$1" = -u ] && echo "${FAKE_UID:-501}" || command id "$@"'
fake df        'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "/dev/disk3 1000000 1 ${FAKE_AVAIL_KB:-50000000} 1% /"'
fake shasum    'shift 2; sha256sum "$@"'
fake xattr     'exit 0'
fake open      'echo "open $*" >> "$STATE/log"; touch "$STATE/running"'
fake osascript 'echo "osascript $*" >> "$STATE/log"; rm -f "$STATE/running"'
fake pgrep     '[ -e "$STATE/running" ]'
fake pkill     'rm -f "$STATE/running"'
fake launchctl 'echo "launchctl $*" >> "$STATE/log"'
fake xcode-select '[ -n "${FAKE_DEVTOOLS:-}" ] && { mkdir -p "$STATE/dev"; echo "$STATE/dev"; exit 0; }; echo "xcode-select: error" >&2; exit 2'
fake defaults  'if [ "$1" = read ]; then f="$2.plist"; k="$3"; [ -f "$f" ] || exit 1; sed -n "s:.*<key>$k</key><string>\([^<]*\)</string>.*:\1:p" "$f"; else echo "defaults $*" >> "$STATE/log"; fi'
fake git       'echo "git $*" >> "$STATE/log"; echo "fatal: (test) no network" >&2; exit 128'
fake ditto     'if [ "$1" = -x ]; then unzip -q "$3" -d "$4"; else cp -R "$1" "$2"; fi'
fake curl      'out=""; url=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; --retry|--retry-delay|--connect-timeout|--max-time) shift;; -*) ;; *) url="$1";; esac; shift; done
echo "curl $url" >> "$STATE/log"
[ -n "${FAKE_OFFLINE:-}" ] && exit 6
case "$url" in
  https://github.com) exit 0;;
  */SHA256SUMS.txt) if [ -n "${FAKE_BAD_SUM:-}" ]; then sed "s/^./0/" "$FIX/SHA256SUMS.txt" > "$out"; else cp "$FIX/SHA256SUMS.txt" "$out"; fi;;
  *.zip) [ -n "${FAKE_NO_RELEASE:-}" ] && exit 22; cp "$FIX/$(basename "$url")" "$out";;
  *) exit 22;;
esac'

# ---------- runner ----------
run() {   # name, expected exit (0/1), then env assignments, then -- installer args
  local name="$1" want="$2"; shift 2
  local envs=(); while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done; [ "${1:-}" = "--" ] && shift
  export OLD_PLIST="${OLD_PLIST:-}" STATE="$T/state-$name"; rm -rf "$STATE"; mkdir -p "$STATE/home"
  [ -n "${PRE:-}" ] && eval "$PRE"
  env HOME="$STATE/home" TMPDIR="$STATE" FIX="$T/fixture" STATE="$STATE" DISKMAP_TEST_PATH="$B" \
      DISKMAP_TEST_SYSTEM_APPS="$STATE/SystemApplications" ${envs[@]+"${envs[@]}"} \
      "${INSTALL_BASH:-bash}" "$ROOT/install.sh" "$@" > "$STATE/out" 2>&1
  GOT=$?
  OUT="$(cat "$STATE/out")"
  if { [ "$want" = 0 ] && [ $GOT -eq 0 ]; } || { [ "$want" != 0 ] && [ $GOT -ne 0 ]; }; then return 0; fi
  echo "    (exit $GOT, wanted $want)"; sed 's/^/    | /' "$STATE/out" | tail -15; return 1
}
check() {   # description, condition…
  local d="$1"; shift
  if "$@"; then pass=$((pass + 1)); printf '  ✓ %s\n' "$d"; else failn=$((failn + 1)); printf '  ✗ %s\n' "$d"; fi
}
has()   { grep -q -- "$1" <<<"$OUT"; }
logged(){ grep -q -- "$1" "$STATE/log" 2>/dev/null; }
app()   { [ -d "$STATE/home/Applications/diskmap.app/Contents/MacOS" ]; }

echo "Apple silicon, fresh install"
PRE="" run fresh 0 && true
check "installs into ~/Applications" app
check "downloads the Apple silicon build" logged "diskmap-macos-arm64.zip"
check "verifies the checksum" logged "SHA256SUMS.txt"
check "opens the app" logged "open "
check "reports the installed version" has "diskmap 9.9.1 is installed"
check "leaves no temp files" bash -c "! ls '$STATE' | grep -q '^diskmap\.'"

echo "Intel Mac"
run intel 0 FAKE_ARM64=0 FAKE_UNAME_M=x86_64
check "downloads the Intel build" logged "diskmap-macos-x86_64.zip"
check "installs it" app

echo "Apple silicon with Terminal running under Rosetta"
run rosetta 0 FAKE_ARM64=1 FAKE_UNAME_M=x86_64
check "still picks the Apple silicon build" logged "diskmap-macos-arm64.zip"

echo "Old macOS"
run oldmac 1 FAKE_OSV=11.7.10
check "refuses with a clear message" has "needs macOS 12"
check "installs nothing" bash -c "! [ -d '$STATE/home/Applications/diskmap.app' ]"

echo "Not a Mac"
run linux 1 FAKE_OS=Linux
check "refuses" has "only runs on a Mac"

echo "Run with sudo"
run root 1 FAKE_UID=0
check "asks to run without sudo" has "without sudo"

echo "Tampered or broken download"
run badsum 1 FAKE_BAD_SUM=1
check "refuses to install it" has "damaged or was changed"
check "installs nothing" bash -c "! [ -d '$STATE/home/Applications/diskmap.app' ]"

echo "Offline"
run offline 1 FAKE_OFFLINE=1
check "says it can't reach GitHub" has "Couldn't reach GitHub"

echo "No release published yet, no developer tools"
run norelease 1 FAKE_NO_RELEASE=1
check "explains there's no download yet" has "no ready-made diskmap download yet"

echo "No release published yet, developer tools installed"
run norelease-dev 1 FAKE_NO_RELEASE=1 FAKE_DEVTOOLS=1
check "falls back to building from source" has "building from source instead"
check "tries to download the source" logged "git clone --depth 1"

echo "Low disk space"
run lowdisk 1 FAKE_AVAIL_KB=100000
check "stops before downloading" has "enough free space"

echo "Updating while diskmap is running"
PRE='mkdir -p "$STATE/home/Applications/diskmap.app/Contents/MacOS" "$STATE/home/Library/Application Support/diskmap"; echo old > "$STATE/home/Applications/diskmap.app/old-marker"; echo keep > "$STATE/home/Library/Application Support/diskmap/history.db"; touch "$STATE/running"' \
  run update 0
check "quits the running copy first" logged "osascript -e tell application id"
check "replaces the old app" bash -c "! [ -e '$STATE/home/Applications/diskmap.app/old-marker' ] && [ -d '$STATE/home/Applications/diskmap.app/Contents/MacOS' ]"
check "keeps history and settings" bash -c "[ -f '$STATE/home/Library/Application Support/diskmap/history.db' ]"
check "reopens it" logged "open "

echo "Existing copy in /Applications only"
OLD_PLIST='<plist><dict><key>CFBundleIdentifier</key><string>local.diskmap.app</string></dict></plist>'
PRE='mkdir -p "$STATE/SystemApplications/diskmap.app/Contents"; printf "%s" "$OLD_PLIST" > "$STATE/SystemApplications/diskmap.app/Contents/Info.plist"; echo old > "$STATE/SystemApplications/diskmap.app/old-marker"' \
  run sysonly 0
check "updates it where it is" bash -c "[ -d '$STATE/SystemApplications/diskmap.app/Contents/MacOS' ] && ! [ -e '$STATE/SystemApplications/diskmap.app/old-marker' ]"
check "doesn't leave a second copy" bash -c "! [ -e '$STATE/home/Applications/diskmap.app' ]"

echo "Copies in both ~/Applications and /Applications"
PRE='mkdir -p "$STATE/SystemApplications/diskmap.app/Contents" "$STATE/home/Applications/diskmap.app/Contents" "$STATE/home/.Trash"; printf "%s" "$OLD_PLIST" > "$STATE/SystemApplications/diskmap.app/Contents/Info.plist"; printf "%s" "$OLD_PLIST" > "$STATE/home/Applications/diskmap.app/Contents/Info.plist"' \
  run both 0
check "updates the one in ~/Applications" app
check "moves the extra copy to the Trash (or says to)" has "copy of diskmap"
check "the extra copy is gone from /Applications" bash -c "! [ -e '$STATE/SystemApplications/diskmap.app' ] || grep -q 'Drag it to the Trash' '$STATE/out'"
PRE=""

echo "Specific version"
run version 0 -- --version 1.2.3
check "downloads that version" logged "releases/download/v1.2.3/diskmap-macos-arm64.zip"

echo "Python inside the download doesn't run"
run pyfail 1 FAKE_PY_FAIL=1
check "stops with an explanation" has "doesn't run on this Mac"
check "installs nothing" bash -c "! [ -d '$STATE/home/Applications/diskmap.app' ]"

echo "Uninstall"
PRE='mkdir -p "$STATE/home/Applications/diskmap.app/Contents" "$STATE/home/Library/LaunchAgents" "$STATE/home/Library/Application Support/diskmap"; printf "<plist><dict><key>CFBundleIdentifier</key><string>local.diskmap.app</string></dict></plist>" > "$STATE/home/Applications/diskmap.app/Contents/Info.plist"; touch "$STATE/home/Library/LaunchAgents/local.diskmap.app.plist" "$STATE/home/Library/Application Support/diskmap/history.db"' \
  run uninstall 0 -- --uninstall
check "removes the app" bash -c "! [ -e '$STATE/home/Applications/diskmap.app' ]"
check "removes Open at Login" bash -c "! [ -e '$STATE/home/Library/LaunchAgents/local.diskmap.app.plist' ]"
check "keeps history unless asked" bash -c "[ -f '$STATE/home/Library/Application Support/diskmap/history.db' ]"
PRE='mkdir -p "$STATE/home/Library/Application Support/diskmap"; touch "$STATE/home/Library/Application Support/diskmap/history.db"' \
  run purge 0 -- --purge
check "--purge deletes history too" bash -c "! [ -e '$STATE/home/Library/Application Support/diskmap' ]"
PRE=""

echo
echo "$pass passed, $failn failed"
[ "$failn" -eq 0 ]
