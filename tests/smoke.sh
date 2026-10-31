#!/usr/bin/env bash
# End-to-end smoke test of the dashboard server, run by CI on a real Mac (and locally on any machine).
#
#   bash tests/smoke.sh [python] [folder with server.py]
#
# Starts the server in demo mode and (on a Mac) in real mode, calls every API, fails on any Python
# traceback, checks nothing is written next to the code, and checks no Apple placeholder tool is run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${1:-python3}"
SRC="${2:-$ROOT}"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$WORK"' EXIT
SHIMS="git python3 pip3 pydoc3 swift swiftc xcodebuild xcrun clang clang++ gcc g++ cc c++ make lipo otool strip nm ld lldb svn javac java"
fails=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗ %s\033[0m\n' "$*"; fails=$((fails + 1)); }

start_server() {   # mode-name, extra args…
  local name="$1"; shift
  export DISKMAP_DATA="$WORK/$name-data" DISKMAP_TOKEN=smoketest DISKMAP_EXEC_LOG="$WORK/$name-exec.log"
  export DISKMAP_ASSUME_NO_DEVTOOLS=1 PYTHONPATH="$ROOT/tests/exec_audit" PYTHONDONTWRITEBYTECODE=1
  mkdir -p "$DISKMAP_DATA"; : > "$DISKMAP_EXEC_LOG"
  "$PY" -B "$SRC/server.py" --port 0 --no-open "$@" > "$WORK/$name.log" 2>&1 &
  PID=$!
  PORT=""
  for _ in $(seq 1 60); do
    PORT="$(grep -o 'DISKMAP_PORT=[0-9]*' "$WORK/$name.log" | head -1 | cut -d= -f2 || true)"
    [ -n "$PORT" ] && break
    kill -0 $PID 2>/dev/null || break
    sleep 0.5
  done
  if [ -z "$PORT" ]; then bad "$name: server didn't start"; cat "$WORK/$name.log"; return 1; fi
  ok "$name: server up on port $PORT"
}

get() { curl -fsS --max-time 30 "http://127.0.0.1:$PORT$1" -o "$WORK/out" && [ -s "$WORK/out" ]; }

check_endpoints() {
  local name="$1" ep
  if get "/" && grep -q "window.DM" "$WORK/out" && grep -q "city-root" "$WORK/out"; then ok "$name: dashboard page"; else bad "$name: dashboard page"; fi
  for ep in "/api/summary" "/api/settings" "/api/diag" "/api/home" "/api/battery" "/api/digest?days=7" \
            "/api/history?range=1h" "/api/history?range=7d" "/api/info?s=security,apps,tools,repos,logs" "/api/procs"; do
    if get "$ep$( [[ $ep == *\?* ]] && echo '&' || echo '?')t=smoketest" && "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$WORK/out" 2>/dev/null; then
      ok "$name: $ep"
    else
      bad "$name: $ep"
    fi
  done
  if curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/summary" -o /dev/null 2>/dev/null; then bad "$name: API answered without the token"; else ok "$name: API refuses requests without the token"; fi
  resp="$(curl -s --max-time 10 -X POST -H 'Content-Type: application/json' -H 'X-Token: smoketest' \
          --data '{"kind":"open_url","url":"https://evil.example/"}' "http://127.0.0.1:$PORT/api/action" || true)"
  if printf '%s' "$resp" | grep -q '"ok": *false'; then ok "$name: refuses to open other websites"; else bad "$name: open_url accepted a foreign URL ($resp)"; fi
}

check_logs() {
  local name="$1" shim hits
  if grep -q "Traceback" "$WORK/$name.log"; then bad "$name: Python error in the log"; grep -A12 "Traceback" "$WORK/$name.log" | head -40; else ok "$name: no Python errors"; fi
  hits=""
  for shim in $SHIMS; do
    if grep -qx "/usr/bin/$shim" "$WORK/$name-exec.log" || grep -qE "^SHELL .*(^|[ ;&|(])$shim( |$)" "$WORK/$name-exec.log"; then hits="$hits $shim"; fi
  done
  if [ -n "$hits" ]; then bad "$name: ran Apple placeholder tools:$hits"; else ok "$name: never runs Apple's developer-tool placeholders ($(sort -u "$WORK/$name-exec.log" | wc -l | tr -d ' ') programs used)"; fi
}

touch "$WORK/marker"; sleep 1
echo "Demo mode"
start_server demo --demo && { sleep 4; check_endpoints demo; }
kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true
check_logs demo

echo "Real mode (pretending this machine has no developer tools)"
# A small fake home folder: a git repo (must NOT be scanned with /usr/bin/git) and an app to inspect.
mkdir -p "$WORK/home/projects/some-repo/.git" "$WORK/home/Applications/Old.app/Contents/MacOS" "$WORK/home/Downloads"
printf '\xcf\xfa\xed\xfe\x07\x00\x00\x01' > "$WORK/home/Applications/Old.app/Contents/MacOS/Old"
printf '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>Old</string></dict></plist>' > "$WORK/home/Applications/Old.app/Contents/Info.plist"
HOME="$WORK/home" start_server real --rescan-min 0 && { sleep 25; check_endpoints real; }
kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true
check_logs real

if [ "$(uname -s)" = "Darwin" ]; then
  echo "Sensors"
  if "$PY" -B "$SRC/macstats.py" > "$WORK/macstats.log" 2>&1; then ok "macstats self-test ran"; else bad "macstats self-test failed"; tail -20 "$WORK/macstats.log"; fi
fi

written="$(find "$SRC" -newer "$WORK/marker" -type f 2>/dev/null | grep -v '/\.git/' || true)"
if [ -n "$written" ]; then bad "files were written next to the code:"; echo "$written" | head; else ok "nothing written next to the code"; fi

echo
if [ "$fails" -gt 0 ]; then echo "✗ $fails check(s) failed"; exit 1; fi
echo "✓ all smoke checks passed"
