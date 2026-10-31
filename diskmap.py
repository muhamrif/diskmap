#!/usr/bin/env python3
"""
diskmap — find out what's eating your Mac's disk, then build a cleanup script.

    python3 diskmap.py            # scan ~, write a report into ./reports, open it
    python3 diskmap.py --no-open  # just write the report
    python3 diskmap.py --root ~/projects --idle-days 14

Read-only: this script never deletes anything. The report lets you pick
items and copies a bash script you can review and run yourself.
Standard library only (Python 3.8+).
"""
import argparse
import datetime
import glob
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
MB = 1000 ** 2

# ---------------------------------------------------------------------------
# Known locations. Paths are globs relative to $HOME.
#   how = "dir"      -> rm -rf the path
#         "contents" -> empty the folder but keep it
#         "manual"   -> only print hints (the "cmd" list), nothing is deleted
# ---------------------------------------------------------------------------
RULES = [
    # ---- safe: caches & build output that regenerate on demand ----
    dict(id="npm", title="npm cache", tier="safe", paths=[".npm/_cacache", ".npm/_npx"],
         note="Re-downloaded on next install."),
    dict(id="yarn", title="Yarn cache", tier="safe", paths=["Library/Caches/Yarn", ".yarn/berry/cache"]),
    dict(id="pnpm", title="pnpm store", tier="safe", paths=["Library/pnpm/store"],
         note="Projects re-link packages on next install."),
    dict(id="bun", title="Bun cache", tier="safe", paths=[".bun/install/cache"]),
    dict(id="gradle", title="Gradle caches", tier="safe",
         paths=[".gradle/caches", ".gradle/wrapper/dists", ".gradle/daemon"],
         note="Next Android/Gradle build will re-download dependencies."),
    dict(id="maven", title="Maven repository", tier="safe", paths=[".m2/repository"]),
    dict(id="pycache", title="uv & pip caches", tier="safe",
         paths=[".cache/uv", "Library/Caches/pip", ".cache/pip", "Library/Caches/pypoetry"]),
    dict(id="browsers", title="Puppeteer & Playwright browsers", tier="safe",
         paths=[".cache/puppeteer", "Library/Caches/ms-playwright"],
         note="Re-downloaded the next time a test or script needs them."),
    dict(id="cocoapods", title="CocoaPods caches", tier="safe",
         paths=["Library/Caches/CocoaPods", ".cocoapods/repos"]),
    dict(id="homebrew", title="Homebrew downloads", tier="safe", paths=["Library/Caches/Homebrew"],
         how="contents", pre=["command -v brew >/dev/null 2>&1 && brew cleanup -s --prune=all || true"]),
    dict(id="gomod", title="Go module cache", tier="safe", paths=["go/pkg/mod"],
         tmpl="chmod -R u+w {q} && rm -rf {q}"),
    dict(id="cargo", title="Cargo registry cache", tier="safe",
         paths=[".cargo/registry/cache", ".cargo/registry/src"]),
    dict(id="xcode-dd", title="Xcode DerivedData", tier="safe",
         paths=["Library/Developer/Xcode/DerivedData"], how="contents",
         note="Xcode rebuilds it; first build of each project will be slower."),
    dict(id="editor", title="VS Code & Cursor caches", tier="safe",
         paths=["Library/Application Support/%s/%s" % (app, sub)
                for app in ("Code", "Cursor", "Windsurf")
                for sub in ("Cache", "CachedData", "CachedExtensionVSIXs", "Service Worker/CacheStorage")],
         note="Quit the editor before running."),
    dict(id="editor-db", title="Cursor / VS Code database backups", tier="safe",
         paths=["Library/Application Support/%s/User/globalStorage/state.vscdb.backup" % a for a in ("Cursor", "Code", "Windsurf")],
         note="Backup copies the editor recreates. Your chats and settings live in state.vscdb, which stays. "
              "To shrink that one too, quit the editor and run Developer › Compact Cursor & VS Code databases."),
    dict(id="trash", title="Trash", tier="safe", paths=[".Trash"], how="contents",
         note="Needs Full Disk Access for Terminal."),

    # ---- review: big, but you might want them ----
    dict(id="android", title="Android SDK & emulators", tier="review",
         paths=["Library/Android/sdk", ".android/avd"],
         note="Reinstall from Android Studio > SDK Manager if you need it again."),
    dict(id="xcode-ds", title="Xcode device support files", tier="review",
         paths=["Library/Developer/Xcode/iOS DeviceSupport", "Library/Developer/Xcode/watchOS DeviceSupport"],
         how="contents", note="Re-created when you plug in a device running that iOS version."),
    dict(id="xcode-arch", title="Xcode archives", tier="review", paths=["Library/Developer/Xcode/Archives"],
         how="contents", note="Old app builds. Keep any you need dSYMs from."),
    dict(id="sims", title="iOS Simulators", tier="review", paths=["Library/Developer/CoreSimulator/Devices"],
         how="manual", cmd=["xcrun simctl delete unavailable", "# erase every simulator's data:  xcrun simctl erase all"],
         note="Size shown is all simulators; the command only removes ones for runtimes you no longer have."),
    dict(id="docker", title="Docker disk image", tier="review",
         paths=["Library/Containers/com.docker.docker/Data/vms"], how="manual",
         cmd=["docker system prune -a --volumes -f"],
         note="Removes unused images, containers and volumes. Docker must be running."),
    dict(id="hf", title="Hugging Face models", tier="review", paths=[".cache/huggingface/hub/models--*"],
         per_item=True),
    dict(id="lmstudio", title="LM Studio models", tier="review",
         paths=[".lmstudio/models/*/*", ".cache/lm-studio/models/*/*"], per_item=True),
]

PROJECT_MARKERS = {".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Package.swift",
                   "pubspec.yaml", "build.gradle", "build.gradle.kts", "Gemfile", "requirements.txt", "Podfile"}
JS_BUILD_DIRS = {".next", ".turbo", ".nuxt", ".svelte-kit", ".parcel-cache", ".angular", ".vercel/output"}
VENV_DIRS = {".venv", "venv"}

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".raw", ".cr2", ".dng", ".mov", ".mp4",
             ".m4v", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".aiff", ".pdf", ".doc", ".docx",
             ".ppt", ".pptx", ".xls", ".xlsx", ".key", ".pages", ".numbers", ".psd", ".epub"}

# Path prefix -> treemap category (longest prefix wins, applies to the whole subtree)
CATEGORY_PREFIX = {
    "Documents": "media", "Pictures": "media", "Movies": "media", "Music": "media", "Downloads": "media",
    "PhotoAlbum": "media", "Public": "media", "Library/Mobile Documents": "media",
    ".ollama": "ai", ".cache/huggingface": "ai", ".lmstudio": "ai", ".cache/lm-studio": "ai",
    "Library/Application Support/LM Studio": "ai",
    "Library/Android": "sdk", ".android": "sdk", "Library/Developer": "sdk", ".swiftpm": "sdk",
    ".npm": "cache", ".gradle": "cache", ".m2": "cache", ".cache": "cache", "Library/Caches": "cache",
    ".cocoapods": "cache", "go/pkg": "cache", ".cargo": "cache", ".bun": "cache", "Library/pnpm": "cache",
    ".yarn": "cache", ".expo": "cache",
}


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit not in ("B", "KB") else ("%d %s" % (n, unit))
        n /= 1000.0


def tilde(p):
    return "~" + p[len(HOME):] if p == HOME or p.startswith(HOME + os.sep) else p


class Scanner:
    def __init__(self, root, idle_days, keep_min=1 * MB, max_depth=9):
        self.root = os.path.abspath(root)
        self.root_dev = os.stat(self.root).st_dev
        self.idle_secs = idle_days * 86400
        self.now = time.time()
        self.keep_min = keep_min
        self.max_depth = max_depth
        self.targets = {}        # abs path -> size (filled during scan)
        self.seen_inodes = set()
        self.unreadable = []
        self.files = 0
        self.dirs = 0
        self.last_tick = 0
        # dynamic findings
        self.node_modules = []   # (project, path, size, last_activity)
        self.js_builds = []      # (project, path, size)
        self.rust_targets = []
        self.venvs = []
        self.worktrees = []      # (path, size)
        self.downloads = []      # (path, size, mtime)
        self.big = []            # (path, blocks, bytes, mtime) for files >= 50 MB

    progress = None  # optional callback(files, path)
    quiet = False

    def tick(self, path):
        t = time.time()
        if t - self.last_tick > 0.25:
            self.last_tick = t
            if self.progress:
                self.progress(self.files, tilde(path))
            if self.quiet:
                return
            short = tilde(path)
            if len(short) > 60:
                short = "…" + short[-59:]
            sys.stderr.write("\r\033[K  %s files · %s  %s" % (format(self.files, ","), short, ""))
            sys.stderr.flush()

    def scan(self, path, depth=0, in_nm=False):
        """Return a node dict for the directory at `path`."""
        self.dirs += 1
        self.tick(path)
        name = os.path.basename(path) or path
        node = {"n": name, "p": path, "d": 1, "s": 0}
        kids, small = [], 0
        try:
            it = os.scandir(path)
        except (PermissionError, OSError):
            self.unreadable.append(path)
            node["u"] = 1
            return node

        entry_names = set()
        latest_mtime = 0
        subdirs = []
        with it:
            for e in it:
                entry_names.add(e.name)
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    if st.st_dev != self.root_dev:
                        continue  # other volume / mount point
                    subdirs.append((e, st))
                    continue
                self.files += 1
                if not stat.S_ISREG(mode):
                    node["s"] += st.st_blocks * 512
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in self.seen_inodes:
                        continue
                    self.seen_inodes.add(key)
                size = st.st_blocks * 512
                node["s"] += size
                if size >= 50 * MB and not in_nm:
                    self.big.append((e.path, size, st.st_size, st.st_mtime))
                if st.st_mtime > latest_mtime:
                    latest_mtime = st.st_mtime
                if size >= self.keep_min and depth < self.max_depth:
                    kids.append({"n": e.name, "s": size})
                else:
                    small += size
                if depth <= 1 and path.startswith(os.path.join(HOME, "Downloads")) and size >= 200 * MB:
                    self.downloads.append((e.path, size, st.st_mtime))

        is_project = bool(entry_names & PROJECT_MARKERS) or any(n.endswith(".xcodeproj") for n in entry_names)
        if is_project:
            node["j"] = 1

        for e, st in subdirs:
            child = self.scan(e.path, depth + 1, in_nm or e.name == "node_modules")
            csize = child["s"]
            node["s"] += csize
            if e.name not in ("node_modules", ".git") and e.name not in JS_BUILD_DIRS and st.st_mtime > latest_mtime:
                latest_mtime = st.st_mtime
            if e.path in self.targets:
                self.targets[e.path] = csize
            if not in_nm:
                self.note_dynamic(path, e.name, e.path, csize, entry_names, is_project)
            if csize >= self.keep_min and depth < self.max_depth:
                kids.append(child)
            else:
                small += csize

        # Track project activity (newest mtime of its own files, ignoring deps/build dirs)
        if is_project:
            for key in ("package.json", ".git"):
                if key in entry_names:
                    try:
                        latest_mtime = max(latest_mtime, os.stat(os.path.join(path, key)).st_mtime)
                        if key == ".git":
                            latest_mtime = max(latest_mtime, os.stat(os.path.join(path, ".git", "index")).st_mtime)
                    except OSError:
                        pass
            node["m"] = latest_mtime

        if small:
            kids.append({"n": "Other files", "s": small, "x": 1})
        kids.sort(key=lambda k: -k["s"])
        node["c"] = kids
        return node

    def note_dynamic(self, parent, name, path, size, siblings, parent_is_project):
        if size < 1 * MB:
            return
        if name == "node_modules" and "package.json" in siblings:
            self.node_modules.append([parent, path, size])
        elif name in JS_BUILD_DIRS and "package.json" in siblings:
            self.js_builds.append((parent, path, size))
        elif name == "target" and "Cargo.toml" in siblings:
            self.rust_targets.append((parent, path, size))
        elif name in VENV_DIRS and parent_is_project:
            self.venvs.append([parent, path, size])
        elif name == "worktrees" and os.path.basename(parent) == ".claude":
            try:
                for e in os.scandir(path):
                    if e.is_dir(follow_symlinks=False):
                        self.worktrees.append(e.path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def index_tree(node, out):
    if node.get("d"):
        out[node["p"]] = node
        for c in node.get("c", []):
            index_tree(c, out)
    return out


def ollama_items(models_dir):
    """Turn ~/.ollama/models manifests into per-model items with real sizes."""
    items = []
    man_root = os.path.join(models_dir, "manifests")
    for path in glob.glob(os.path.join(man_root, "*", "*", "*", "*")):
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        size = sum(l.get("size", 0) for l in m.get("layers", [])) + m.get("config", {}).get("size", 0)
        reg, ns, model, tag = os.path.relpath(path, man_root).split(os.sep)
        name = ("%s:%s" % (model, tag)) if ns == "library" else ("%s/%s:%s" % (ns, model, tag))
        if reg != "registry.ollama.ai":
            name = "%s/%s" % (reg, name)
        items.append({"label": name, "p": tilde(path), "s": size, "cmds": ["ollama rm %s" % shlex.quote(name)]})
    items.sort(key=lambda i: -i["s"])
    return items


def cmds_for(path, how="dir", tmpl=None):
    q = shlex.quote(path)
    if tmpl:
        return [tmpl.format(q=q)]
    if how == "contents":
        return ["find %s -mindepth 1 -maxdepth 1 -exec rm -rf {} +" % q]
    return ["rm -rf %s" % q]


def project_label(project, path):
    rel = os.path.relpath(path, os.path.dirname(project))
    return rel


def build_recs(sc, tree_index, idle_days):
    recs = []
    claimed = set()

    for r in RULES:
        how = r.get("how", "dir")
        items = []
        for pattern in r["paths"]:
            for p in sorted(glob.glob(os.path.join(HOME, pattern))):
                size = sc.targets.get(p)
                if size is None:
                    node = tree_index.get(p)
                    size = node["s"] if node else None
                if not size or size < 1 * MB:
                    continue
                claimed.add(p)
                label = os.path.basename(p) if r.get("per_item") else tilde(p)
                if r["id"] == "hf":
                    label = os.path.basename(p).replace("models--", "").replace("--", "/")
                items.append({"label": label, "p": tilde(p), "abs": p, "s": size,
                              "cmds": [] if how == "manual" else cmds_for(p, how, r.get("tmpl"))})
        if not items:
            continue
        rec = {"id": r["id"], "title": r["title"], "tier": r["tier"], "note": r.get("note", ""),
               "items": items, "pre": r.get("pre", []), "manual": how == "manual"}
        if how == "manual":
            rec["post"] = r.get("cmd", [])
        recs.append(rec)

    # Ollama: per-model, using `ollama rm`
    om = os.path.join(HOME, ".ollama", "models")
    if os.path.isdir(om):
        items = ollama_items(om)
        if items:
            claimed.add(om)
            for i in items:
                i["abs"] = om  # tile highlight for the whole models dir
            recs.append({"id": "ollama", "title": "Ollama models", "tier": "review",
                         "note": "Removed with `ollama rm` so Ollama's index stays consistent. "
                                 "Shared layers are freed once no model uses them.",
                         "items": items, "pre": [], "manual": False, "hl": [om]})

    def under_claimed(p):
        return any(p == c or p.startswith(c + os.sep) for c in claimed)

    # JS build output
    items = [{"label": project_label(proj, p), "p": tilde(p), "abs": p, "s": s, "cmds": cmds_for(p)}
             for proj, p, s in sc.js_builds if not under_claimed(p)]
    if items:
        recs.append({"id": "jsbuild", "title": "Next.js / Turbo build output", "tier": "safe",
                     "note": "Build caches (.next, .turbo, .nuxt…). Rebuilt on next dev/build.",
                     "items": sorted(items, key=lambda i: -i["s"]), "pre": [], "manual": False})

    items = [{"label": project_label(proj, p), "p": tilde(p), "abs": p, "s": s, "cmds": cmds_for(p)}
             for proj, p, s in sc.rust_targets if not under_claimed(p)]
    if items:
        recs.append({"id": "rust", "title": "Rust target/ folders", "tier": "safe",
                     "note": "Same as `cargo clean`.", "items": sorted(items, key=lambda i: -i["s"]),
                     "pre": [], "manual": False})

    def idle(proj):
        node = tree_index.get(proj)
        m = node.get("m", 0) if node else 0
        return m and (sc.now - m) > sc.idle_secs, m

    for key, title, lst in (("nm", "node_modules in idle projects", sc.node_modules),
                            ("venv", "Python venvs in idle projects", sc.venvs)):
        items = []
        for proj, p, s in lst:
            if under_claimed(p):
                continue
            is_idle, m = idle(proj)
            if not is_idle:
                continue
            days = int((sc.now - m) / 86400)
            items.append({"label": "%s  ·  %dd idle" % (project_label(proj, p), days), "p": tilde(p),
                          "abs": p, "s": s, "cmds": cmds_for(p)})
        if items:
            recs.append({"id": key, "title": title, "tier": "review",
                         "note": "Projects untouched for %d+ days. Reinstall with npm/pnpm install or uv sync." % idle_days,
                         "items": sorted(items, key=lambda i: -i["s"]), "pre": [], "manual": False})

    items = []
    for p in sc.worktrees:
        node = tree_index.get(p)
        s = node["s"] if node else 0
        if s >= 1 * MB and not under_claimed(p):
            repo = os.path.dirname(os.path.dirname(os.path.dirname(p)))
            items.append({"label": "%s / %s" % (os.path.basename(repo), os.path.basename(p)), "p": tilde(p),
                          "abs": p, "s": s,
                          "cmds": ["git -C %s worktree remove --force %s 2>/dev/null || rm -rf %s"
                                   % (shlex.quote(repo), shlex.quote(p), shlex.quote(p))]})
    if items:
        recs.append({"id": "worktrees", "title": "Claude Code worktrees", "tier": "review",
                     "note": "Agent worktrees under .claude/worktrees. Check nothing uncommitted is left in them.",
                     "items": sorted(items, key=lambda i: -i["s"]), "pre": [],
                     "manual": False, "post": ["# afterwards, in each repo:  git worktree prune"]})

    old = 30 * 86400
    items = [{"label": "%s  ·  %dd old" % (os.path.basename(p), int((sc.now - m) / 86400)), "p": tilde(p),
              "abs": p, "s": s, "cmds": cmds_for(p)}
             for p, s, m in sc.downloads if (sc.now - m) > old]
    if items:
        recs.append({"id": "downloads", "title": "Large old downloads", "tier": "review",
                     "note": "Files over 200 MB in Downloads, untouched for 30+ days.",
                     "items": sorted(items, key=lambda i: -i["s"]), "pre": [], "manual": False})

    for r in recs:
        r["s"] = sum(i["s"] for i in r["items"])
        if "hl" not in r:
            r["hl"] = [i["abs"] for i in r["items"]]
        for i in r["items"]:
            i.pop("abs", None)
    recs = [r for r in recs if r["s"] >= 10 * MB]
    recs.sort(key=lambda r: (r["tier"] != "safe", -r["s"]))
    return recs


def categorize(node, rel, inherited=None):
    """Assign treemap category `k` to every node; returns the node's category."""
    k = inherited
    if node.get("d") and rel is not None:
        best = None
        for pre, cat in CATEGORY_PREFIX.items():
            if rel == pre or rel.startswith(pre + "/"):
                if best is None or len(pre) > len(best[0]):
                    best = (pre, cat)
        if best:
            k = best[1]
        elif not k and node.get("j"):
            k = "code"
    kids = node.get("c", [])
    weights = {}
    for c in kids:
        crel = (rel + "/" + c["n"] if rel else c["n"]) if c.get("d") else None
        if c.get("d"):
            ck = categorize(c, crel, k)
        else:
            ext = os.path.splitext(c["n"])[1].lower()
            ck = k or ("media" if ext in MEDIA_EXT else "other")
            c["k"] = ck
        weights[ck] = weights.get(ck, 0) + c["s"]
    if not k:
        k = max(weights, key=weights.get) if weights else "other"
        if node.get("x"):
            k = inherited or "other"
    node["k"] = k
    return k


def prune(node, min_size, max_kids=48):
    kids = node.get("c")
    if not kids:
        node.pop("c", None)
        return
    keep, rest = [], 0
    for c in kids:
        if (c["s"] >= min_size and len(keep) < max_kids) and not c.get("x"):
            keep.append(c)
        else:
            rest += c["s"]
    if rest:
        keep.append({"n": "Other", "s": rest, "x": 1, "k": node.get("k", "other")})
    node["c"] = keep
    for c in keep:
        if c.get("d"):
            prune(c, min_size, max_kids)


def attach_recs(tree_index, recs):
    for r in recs:
        for p in r["hl"]:
            n = tree_index.get(p)
            if n is None:  # a file: find it among its folder's children
                parent = tree_index.get(os.path.dirname(p))
                name = os.path.basename(p)
                n = next((c for c in (parent or {}).get("c", []) if c["n"] == name and not c.get("d")), None)
            if n:
                n["r"] = r["id"]
        r["hl"] = [tilde(p) for p in r["hl"]]


def finalize_paths(node):
    if node.get("d"):
        node["p"] = tilde(node["p"])
        node.pop("m", None)
        node.pop("j", None)
        for c in node.get("c", []):
            finalize_paths(c)


QUIET = [False]


def measure(path):
    """Size of one path on disk (0 if it no longer exists)."""
    if not os.path.lexists(path):
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_blocks * 512
        except OSError:
            return 0
    sc = Scanner(path, 30, keep_min=1 << 62, max_depth=0)
    sc.quiet = True
    return sc.scan(path)["s"]


def run_scan(root=HOME, idle_days=30, progress=None, quiet=False):
    """Scan `root` and return the report data (same shape the UI expects)."""
    QUIET[0] = quiet
    sys.setrecursionlimit(20000)
    root = os.path.abspath(os.path.expanduser(root))
    sc = Scanner(root, idle_days)
    sc.progress, sc.quiet = progress, quiet
    for r in RULES:
        for pattern in r["paths"]:
            for p in glob.glob(os.path.join(HOME, pattern)):
                sc.targets[p] = None

    if not quiet:
        print("diskmap · scanning %s" % tilde(root), file=sys.stderr)
    t0 = time.time()
    tree = sc.scan(root)
    tree["n"] = tilde(root) if root != HOME else "~ Home"
    secs = time.time() - t0
    if not quiet:
        sys.stderr.write("\r\033[K  %s files in %.0fs — %s\n" % (format(sc.files, ","), secs, human(tree["s"])))

    index = index_tree(tree, {})
    recs = build_recs(sc, index, idle_days)
    if progress:
        progress(sc.files, "checking large files for duplicates")
    large, dups = big_files(sc)
    categorize(tree, "" if root == HOME else os.path.relpath(root, HOME))
    attach_recs(index, recs)
    prune(tree, max(tree["s"] * 0.0005, 5 * MB))
    finalize_paths(tree)

    if progress:
        progress(sc.files, "measuring apps & system")
    du = shutil.disk_usage(root)
    outside = outside_home(tree["s"], du.used) if root == HOME else None
    return {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname().split(".")[0],
        "root": tilde(root),
        "home": HOME,
        "disk": {"total": du.total, "used": du.used, "free": du.free},
        "scanned": tree["s"],
        "files": sc.files,
        "seconds": round(secs, 1),
        "unreadable": [tilde(p) for p in sc.unreadable[:200]],
        "unreadable_count": len(sc.unreadable),
        "tree": tree,
        "outside": outside,
        "recs": recs,
        "large": large,
        "dups": dups,
    }


def _partial_hash(path, n=1 << 20):
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            h.update(f.read(n))
            size = os.fstat(f.fileno()).st_size
            if size > 2 * n:
                f.seek(size // 2)
                h.update(f.read(n))
                f.seek(-n, 2)
                h.update(f.read(n))
    except OSError:
        return None
    return h.hexdigest()


def big_files(sc, limit=150):
    """Largest files, plus groups of likely duplicates (same size + same sampled content)."""
    now = time.time()
    big = sorted(sc.big, key=lambda b: -b[1])
    large = [{"p": tilde(p), "s": s, "age": int((now - m) / 86400), "ext": os.path.splitext(p)[1].lower()}
             for p, s, _, m in big[:limit]]
    by_size = {}
    for p, s, n, m in big:
        by_size.setdefault(n, []).append((p, s, m))
    dups = []
    for n, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash = {}
        for p, s, m in files:
            h = _partial_hash(p)
            if h:
                by_hash.setdefault(h, []).append((p, s, m))
        for grp in by_hash.values():
            if len(grp) > 1:
                grp.sort(key=lambda g: g[2])  # oldest first = the "original"
                dups.append({"size": grp[0][1], "waste": grp[0][1] * (len(grp) - 1),
                             "files": [{"p": tilde(p), "age": int((now - m) / 86400)} for p, s, m in grp]})
    dups.sort(key=lambda d: -d["waste"])
    return large, dups[:60]


def render_html(data, live=None):
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/") if data else "null"
    html = html.replace("/*__DISKMAP_DATA__*/null", payload)
    html = html.replace("/*__DISKMAP_LIVE__*/null", json.dumps(live) if live else "null")
    for name in ("home", "city"):
        part = os.path.join(HERE, name + ".html")
        if live and os.path.exists(part):
            with open(part, encoding="utf-8") as f:
                html = html.replace("<!--include:%s-->" % name, f.read())
    return html


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def apfs_layout():
    """Per-volume usage of the APFS container that holds the startup disk (macOS only)."""
    if sys.platform != "darwin":
        return None
    import plistlib
    try:
        info = plistlib.loads(_run(["diskutil", "info", "-plist", "/"]))
        ref = info.get("APFSContainerReference")
        lst = plistlib.loads(_run(["diskutil", "apfs", "list", "-plist"]))
    except Exception:
        return None
    for c in lst.get("Containers", []):
        if c.get("ContainerReference") == ref:
            vols = [{"name": v.get("Name", "?"), "roles": v.get("Roles", []), "used": v.get("CapacityInUse", 0)}
                    for v in c.get("Volumes", [])]
            return {"capacity": c.get("CapacityCeiling", 0), "free": c.get("CapacityFree", 0), "volumes": vols}
    return None


def local_snapshots():
    if sys.platform != "darwin":
        return []
    out = _run(["tmutil", "listlocalsnapshots", "/"]).decode(errors="replace")
    return [l.strip() for l in out.splitlines() if "com.apple" in l]


def outside_home(home_scanned, disk_used):
    """Explain the space that isn't in the home folder."""
    roots = [("Applications", "/Applications"), ("System Library", "/Library"),
             ("Homebrew", "/opt/homebrew"), ("/usr/local", "/usr/local"),
             ("Temp & per-app caches", "/private/var/folders"),
             ("Shared user folder", "/Users/Shared")]
    users = os.path.dirname(HOME)
    try:
        for e in os.scandir(users):
            if e.is_dir(follow_symlinks=False) and e.path != HOME and e.name not in ("Shared", "Guest", ".localized"):
                roots.append(("Other user: " + e.name, e.path))
    except OSError:
        pass

    items, measured = [], 0
    for label, path in roots:
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if not QUIET[0]:
            sys.stderr.write("\r\033[K  measuring %s" % path)
        sc = Scanner(path, 30, keep_min=1 << 62, max_depth=0)  # sizes only, no tree
        sc.quiet = QUIET[0]
        try:
            node = sc.scan(path)
        except RecursionError:
            continue
        if node["s"] < 50 * MB:
            continue
        items.append({"label": label, "p": path, "s": node["s"], "unreadable": len(sc.unreadable)})
        measured += node["s"]
    if not QUIET[0]:
        sys.stderr.write("\r\033[K")

    apfs = apfs_layout()
    snaps = local_snapshots()
    rest_label = "Unreadable folders, snapshots & purgeable"
    if apfs:
        data_used = 0
        for v in apfs["volumes"]:
            roles = v["roles"]
            if "Data" in roles:
                data_used += v["used"]
            elif v["used"] >= 50 * MB:
                role = roles[0] if roles else ""
                desc = {"System": "macOS system volume", "Preboot": "Boot files", "Recovery": "Recovery",
                        "VM": "Virtual memory (swap)", "Update": "Pending macOS update"}.get(role, "Other volume")
                items.append({"label": desc, "p": "volume: " + v["name"], "s": v["used"], "unreadable": 0})
        rest = data_used - home_scanned - measured
    else:
        rest = disk_used - home_scanned - measured
    if rest > 50 * MB:
        items.append({"label": rest_label, "p": "", "s": rest, "unreadable": 0, "rest": 1})
    items.sort(key=lambda i: (i.get("rest", 0), -i["s"]))
    return {"items": items, "snapshots": len(snaps), "apfs": bool(apfs)}


def main():
    ap = argparse.ArgumentParser(description="Map your disk and build a cleanup script.")
    ap.add_argument("--root", default=HOME, help="folder to scan (default: your home folder)")
    ap.add_argument("--idle-days", type=int, default=30, help="project counts as idle after N days (default 30)")
    ap.add_argument("--out", default=os.path.join(HERE, "reports"), help="where to write the report")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    ap.add_argument("--json", action="store_true", help="also write the raw scan JSON")
    args = ap.parse_args()
    sys.setrecursionlimit(20000)

    root = os.path.abspath(os.path.expanduser(args.root))
    data = run_scan(root, args.idle_days)
    recs = data["recs"]

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    html = render_html(data)
    out = os.path.join(args.out, "diskmap-%s.html" % stamp)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    if args.json:
        with open(out[:-5] + ".json", "w") as f:
            json.dump(data, f, indent=1)

    safe = sum(r["s"] for r in recs if r["tier"] == "safe")
    review = sum(r["s"] for r in recs if r["tier"] == "review")
    print("  safe to remove: %s · needs review: %s" % (human(safe), human(review)), file=sys.stderr)
    if data["unreadable_count"]:
        print("  %d folders were unreadable — give your terminal Full Disk Access for a complete map"
              % data["unreadable_count"], file=sys.stderr)
    print("  report: %s" % out, file=sys.stderr)

    if not args.no_open:
        if sys.platform == "darwin":
            subprocess.call(["open", out])
        else:
            import webbrowser
            webbrowser.open("file://" + out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\naborted\n")
        sys.exit(130)
