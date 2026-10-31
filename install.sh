#!/usr/bin/env bash
# Installs (or updates) diskmap: downloads the source from GitHub, builds the app on this Mac
# and puts it in ~/Applications. Run the same command again any time to update.
#
#   curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/diskmap/main/install.sh | bash
#
set -euo pipefail
REPO="${DISKMAP_REPO:-YOUR_GITHUB_USERNAME/diskmap}"
SRC="${DISKMAP_SRC:-$HOME/.diskmap/src}"

say(){ printf '\033[1m%s\033[0m\n' "$*"; }

[ "$(uname -s)" = "Darwin" ] || { echo "diskmap only runs on macOS."; exit 1; }
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 12 ] || { echo "diskmap needs macOS 12 (Monterey) or newer."; exit 1; }

# Apple's free Command Line Tools give us git, Swift and Python. Nothing else is needed.
if ! xcode-select -p >/dev/null 2>&1 || ! command -v swiftc >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  say "diskmap needs Apple's free Command Line Tools (git, Swift and Python)."
  echo "A window will open now. Click Install, wait for it to finish (a few minutes),"
  echo "then run the same install command again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi

if [ -d "$SRC/.git" ]; then
  say "→ Updating diskmap"
  git -C "$SRC" pull --ff-only --quiet
else
  say "→ Downloading diskmap"
  mkdir -p "$(dirname "$SRC")"
  git clone --depth 1 --quiet "https://github.com/$REPO.git" "$SRC"
fi

bash "$SRC/macapp/build.sh"

cat <<'MSG'

✓ diskmap is installed in ~/Applications and should be open now.

First-time setup (System Settings › Privacy & Security):
  • Full Disk Access → add diskmap (for the complete disk map). Reopen diskmap after.
  • Automation → click Allow when diskmap asks about Safari/Chrome (tabs), Finder (Trash)
    and System Events (login items).
  • Notifications → allow diskmap.
Then open diskmap's Diagnostics tab: every check should be green or have a Fix button.

To update later, run the same install command again.
MSG
