#!/usr/bin/env python3
"""
diskmap live — a local dashboard for your Mac: disk map, CPU / GPU / memory,
the apps using them, browser tabs, dev servers, loaded AI models, and
suggested actions you can run with one click.

    python3 server.py                 # http://127.0.0.1:7777 (opens automatically)
    python3 server.py --port 8080 --no-open
    python3 server.py --lan           # also viewable from other devices (read-only there)

Standard library only. Nothing is deleted or quit without you clicking
(and confirming) the action in the page.
"""
import argparse
import collections
import json
import os
import plistlib
import pwd
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import diskmap as dm
import history as histmod
import inventory as inv
import macstats

MAC = sys.platform == "darwin"
ME = os.getuid()
ME_NAME = pwd.getpwuid(ME).pw_name
MYPID = os.getpid()
HOME = dm.HOME
CACHE = os.path.join(dm.HERE, "reports", "last-scan.json")
MB, GB = 1000 ** 2, 1000 ** 3

# Apps never suggested for quitting (the terminal running this server, system UI…)
PROTECTED = {"Finder", "Dock", "WindowServer", "loginwindow", "SystemUIServer", "Control Center",
             "Notification Center", "Spotlight", "Terminal", "iTerm", "iTerm2", "Ghostty", "Warp", "Alacritty",
             "kitty", "WezTerm", "Claude", "Activity Monitor", "System Settings"}
CHROMIUM = ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Vivaldi", "Chromium", "Google Chrome Canary"]
BROWSERS = CHROMIUM + ["Safari"]
HEAVY_SITES = ["youtube.com", "figma.com", "meet.google.com", "teams.microsoft.com", "teams.live.com", "docs.google.com",
               "sheets.google.com", "slides.google.com", "notion.so", "twitch.tv", "netflix.com", "canva.com",
               "colab.research.google.com", "app.slack.com", "discord.com", "web.whatsapp.com", "x.com", "twitter.com",
               "instagram.com", "facebook.com", "mail.google.com", "outlook.office.com", "outlook.live.com",
               "miro.com", "linear.app", "chatgpt.com", "claude.ai", "zoom.us", "primevideo.com", "hulu.com",
               "databricks.com", "grafana", "tradingview.com", "sleeper.com"]
SYSTEM_EXPLAIN = {
    "mds_stores": "Spotlight is indexing files. It settles down on its own.",
    "mds": "Spotlight is indexing files. It settles down on its own.",
    "mdworker_shared": "Spotlight is indexing files. It settles down on its own.",
    "corespotlightd": "Spotlight is indexing app content. It settles down on its own.",
    "kernel_task": "macOS is holding the CPU back to cool down. A heavy task or a hot room is usually the cause.",
    "WindowServer": "Draws everything on screen. Lots of windows, displays or animations push it up.",
    "photoanalysisd": "Photos is analysing your library. It mostly runs while the Mac is idle.",
    "mediaanalysisd": "Photos is analysing your library. It mostly runs while the Mac is idle.",
    "backupd": "A Time Machine backup is running.",
    "cloudd": "iCloud is syncing.",
    "bird": "iCloud Drive is syncing.",
    "fileproviderd": "A cloud drive (iCloud, Google Drive, Dropbox…) is syncing files.",
    "softwareupdated": "macOS is downloading or preparing an update.",
    "XprotectService": "macOS malware scan. Short-lived.",
    "trustd": "Checking certificates. Usually brief.",
}
DEV_RUNTIMES = {"node", "python", "python3", "Python", "ruby", "java", "bun", "deno", "go", "php", "uvicorn",
                "gunicorn", "next-server", "vite", "esbuild", "postgres", "redis-server", "mongod", "mysqld",
                "ollama", "turbo", "dotnet", "beam.smp", "rails", "flask", "docker-proxy"}


def sh(cmd, timeout=10, inp=None):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, input=inp)
        return r.stdout, r.stderr, r.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return "", str(e), -1


def out(cmd, timeout=10):
    return sh(cmd, timeout)[0]


def sysctl(name):
    return out(["sysctl", "-n", name]).strip()


def etime_secs(e):
    """ps etime '[[dd-]hh:]mm:ss' -> seconds."""
    days = 0
    if "-" in e:
        d, e = e.split("-", 1)
        days = int(d)
    parts = [int(x) for x in e.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_size(s):
    """'1234M+', '12K', '1.5G' -> bytes (binary units, as top prints them)."""
    s = s.rstrip("+-")
    m = re.match(r"([\d.]+)([BKMGT]?)", s)
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)])


def app_of(path):
    """Outermost .app bundle in an executable path, or None."""
    i = path.find(".app/")
    if i < 0:
        return path if path.endswith(".app") else None
    return path[:i + 4]


def domain_of(url):
    try:
        h = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def as_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(script, timeout=8):
    return sh(["osascript", "-"], timeout=timeout, inp=script)


# ---------------------------------------------------------------------------
# Broadcast to connected pages (Server-Sent Events)
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.last = {}

    def add(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.append(q)
        return q

    def remove(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, event, data, remember=True):
        msg = ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, separators=(",", ":")))).encode()
        with self.lock:
            if remember:
                self.last[event] = msg
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


HUB = Hub()
DATA_DIR = os.environ.get("DISKMAP_DATA") or os.path.join(dm.HERE, "data")
if os.environ.get("DISKMAP_DATA"):  # running inside the Mac app: keep everything in Application Support
    CACHE = os.path.join(DATA_DIR, "last-scan.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DEFAULT_SETTINGS = {"notify": True, "notify_temp": 95, "notify_disk_gb": 10, "notify_battery": 10,
                    "notify_pressure": True, "notify_hog": True, "notify_swap": True, "notify_crash": True,
                    "notify_done": True, "public_ip": False, "history_days": 30,
                    "auto_clean": False, "auto_models": False, "auto_tabs": False, "last_auto_clean": 0,
                    "idle_interval": 10, "notify_growth": True, "growth_gb": 2,
                    "digest_on": True, "digest_day": 6, "digest_hour": 18, "last_digest": 0,
                    "watch_on": True, "watch_drop_gb": 5,
                    "ui_city": "nyc", "ui_sky": "auto", "ui_labels": "values", "ui_theme": "auto",
                    "profiles": [
                        {"name": "Deep work", "icon": "🎧", "quit": ["Slack", "Discord", "Messages", "WhatsApp", "Telegram", "Music", "Spotify", "Mail", "News", "TV"],
                         "unload_models": False, "close_heavy_tabs": False, "shortcut_on": "", "shortcut_off": ""},
                        {"name": "Meeting", "icon": "📞", "quit": ["Xcode", "Simulator", "Android Studio", "Docker", "Steam", "Final Cut Pro", "Logic Pro"],
                         "unload_models": True, "close_heavy_tabs": False, "shortcut_on": "", "shortcut_off": ""},
                        {"name": "Free up memory", "icon": "🧹", "quit": ["Docker", "Simulator", "Android Studio", "Steam"],
                         "unload_models": True, "close_heavy_tabs": True, "shortcut_on": "", "shortcut_off": ""}],
                    "focus_active": {}}
APP_MODE = bool(os.environ.get("DISKMAP_DATA"))


def load_settings():
    st = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH) as f:
            st.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return st


SETTINGS = load_settings()
ERRLOG = collections.deque(maxlen=60)


class _Tee:
    def __init__(self, real):
        self.real = real

    def write(self, s):
        if s.strip():
            ERRLOG.append({"t": time.time(), "msg": s.strip()[:300]})
        return self.real.write(s)

    def flush(self):
        self.real.flush()


sys.stderr = _Tee(sys.stderr)
HISTORY = None
INV = None


def save_settings():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(SETTINGS, f, indent=1)


class Notifier:
    """Alerts when something needs attention. Inside the Mac app they become native notifications
    (with action buttons); in the browser version they fall back to osascript."""

    def __init__(self):
        self.last = {}
        self.streak = collections.Counter()
        self.queue = collections.deque(maxlen=50)
        self.seq = 0
        self.seen_crash = None

    def push(self, kind, title, body="", action=None, cooldown=1800, setting="notify", num=None):
        if not SETTINGS.get("notify") or not SETTINGS.get(setting, True):
            return
        now = time.time()
        if cooldown and now - self.last.get(kind, 0) < cooldown:
            return
        self.last[kind] = now
        self.seq += 1
        self.queue.append({"seq": self.seq, "kind": kind, "title": title, "body": body, "action": action, "t": now})
        if HISTORY:
            HISTORY.event(kind, title + (" — " + body if body else ""), num)
        HUB.send("note", self.queue[-1], remember=False)
        if MAC and not APP_MODE:
            osascript('display notification %s with title "diskmap" subtitle %s sound name "Submarine"'
                      % (as_quote(body or title), as_quote(title if body else "")), timeout=5)

    def since(self, seq):
        return [n for n in self.queue if n["seq"] > seq]

    def check(self, s, groups):
        hw = s.get("hw") or {}
        t = (hw.get("temps") or {}).get("cpu_max")
        bat = hw.get("battery") or {}
        pg = hw.get("paging") or {}
        hog = max((g for g in groups.values() if g.get("mine") and not g.get("protected") and g.get("app")),
                  key=lambda g: g["cpu_avg"], default=None)
        quit_hog = {"label": "Quit %s" % hog["name"], "act": {"kind": "quit_app", "app": hog["app"]}} if hog else None
        conds = [
            ("temp", t is not None and t >= SETTINGS["notify_temp"], 5, "notify",
             "Your Mac is running hot", "CPU at %s °C%s" % (round(t) if t else "?", (", %s is busiest" % hog["name"]) if hog and hog["cpu_avg"] > 50 else ""),
             quit_hog if hog and hog["cpu_avg"] > 50 else None),
            ("disk", s["disk"]["free"] < SETTINGS["notify_disk_gb"] * GB, 5, "notify",
             "Disk almost full", "%s free" % fmt(s["disk"]["free"]), {"label": "Free up space", "open": "storage"}),
            ("battery", bat.get("percent") is not None and not bat.get("plugged") and bat["percent"] <= SETTINGS["notify_battery"], 5,
             "notify", "Battery at %s%%" % bat.get("percent"), "Plug in soon.", None),
            ("pressure", SETTINGS.get("notify_pressure") and s["mem"]["pressure"] == "critical", 5, "notify_pressure",
             "Memory pressure is critical", "Quit something big to speed things up.", {"label": "Show apps", "open": "overview"}),
            ("swap", pg.get("swapouts", 0) > 20 * MB, 10, "notify_swap",
             "Your Mac is swapping heavily", "Writing %s/s of memory to disk." % fmt(pg.get("swapouts", 0)), {"label": "Show apps", "open": "overview"}),
            ("hog", bool(hog) and hog["cpu_avg"] >= 150 and hog["samples"] >= 15 and not hog.get("front"), 75, "notify_hog",
             "%s has been using %d%% CPU" % (hog["name"], hog["cpu_avg"]) if hog else "", "For several minutes, in the background.", quit_hog),
        ]
        for kind, on, need, setting, title, body, action in conds:
            self.streak[kind] = self.streak[kind] + 1 if on else 0
            if self.streak[kind] >= need:
                self.push(kind, title, body, action, setting=setting)

    def check_logs(self, logs):
        crashes = (logs or {}).get("crashes") or []
        newest = max((c["ts"] for c in crashes), default=0)
        if self.seen_crash is None:
            self.seen_crash = newest
            return
        fresh = [c for c in crashes if c["ts"] > self.seen_crash]
        self.seen_crash = max(self.seen_crash, newest)
        for c in fresh[:2]:
            what = {"panic": "Your Mac restarted after a kernel panic", "hang": "%s stopped responding" % c["app"]}.get(
                c["kind"], "%s crashed" % c["app"])
            self.push("crash:" + c["app"], what, "Report saved in Logs.", {"label": "See logs", "open": "logs"},
                      cooldown=600, setting="notify_crash")


NOTIFIER = Notifier()


class Autopilot:
    """Opt-in automatic actions (all off by default; turn on in Settings)."""

    def __init__(self):
        self.pressure_streak = 0
        self.last = {}

    def tick(self, s, sampler, groups):
        now = time.time()
        tight = s["mem"]["pressure"] != "normal"
        self.pressure_streak = self.pressure_streak + 1 if tight else 0
        if self.pressure_streak >= 30 and now - self.last.get("mem", 0) > 600:  # ~1 min of pressure
            self.last["mem"] = now
            done = []
            if SETTINGS.get("auto_models"):
                for m in list(sampler.models):
                    try:
                        act({"kind": "unload_model", "model": m["name"]}, sampler, None)
                        done.append("unloaded %s" % m["name"])
                    except Exception:
                        pass
            if SETTINGS.get("auto_tabs"):
                for b, info in (sampler.tabs or {}).items():
                    dups = [{"win": t["win"], "id": t["id"], "url": t["url"]} for t in info.get("tabs", []) if t.get("dup")]
                    if dups:
                        try:
                            n = close_tabs(b, dups)
                            done.append("closed %d duplicate tab%s in %s" % (n, "" if n == 1 else "s", b))
                        except Exception:
                            pass
            if done:
                txt = "; ".join(done)
                NOTIFIER.push("autopilot-mem", "Autopilot freed memory", txt[0].upper() + txt[1:] + ".",
                              {"label": "Open", "open": "overview"}, cooldown=0, setting="notify_done")
        if now - self.last.get("digest", 0) > 600:
            self.last["digest"] = now
            check_digest()
        if SETTINGS.get("auto_clean") and now - self.last.get("clean_check", 0) > 300:
            self.last["clean_check"] = now
            self.maybe_clean(s)

    def maybe_clean(self, s):
        disk = Handler.disk
        if not disk or not disk.data or disk.cleaning or disk.scanning:
            return
        if time.time() - float(SETTINGS.get("last_auto_clean", 0)) < 7 * 86400:
            return
        bat = ((s.get("hw") or {}).get("battery") or {})
        if bat.get("percent") is not None and not bat.get("plugged"):
            return  # only on power
        if s["cpu"]["total"] > 40:
            return  # only when the Mac is quiet
        picks = [[r["id"], i] for r in disk.data["recs"] if r["tier"] == "safe" for i in range(len(r["items"]))]
        if not picks:
            return
        SETTINGS["last_auto_clean"] = time.time()
        save_settings()
        try:
            disk.clean(picks, disk.data["scan_id"])
            if HISTORY:
                HISTORY.event("autopilot", "Weekly cleanup of %d safe items started" % len(picks))
        except Exception as e:
            print("autopilot clean:", e, file=sys.stderr)


AUTOPILOT = Autopilot()


def dur_text(sec):
    sec = int(sec or 0)
    if sec >= 2 * 86400:
        return "%d days" % (sec // 86400)
    if sec >= 3600:
        return "%d h" % (sec // 3600)
    return "%d min" % max(1, sec // 60)


class GrowthTracker:
    """Finds apps whose memory keeps climbing the longer they run (leaks, tab bloat)."""

    def __init__(self):
        self.result = []
        self.last = 0

    def tick(self, sampler, groups):
        if not HISTORY or time.time() - self.last < 300:
            return
        self.last = time.time()
        threading.Thread(target=self._run, args=(dict(groups),), daemon=True).start()

    def _run(self, groups):
        res = []
        now = time.time()
        for g in groups.values():
            if g["kind"] != "app" or g["mem"] < 700 * MB or g.get("age", 0) < 3 * 3600:
                continue
            rows = HISTORY.mem_since(g["name"], now - min(g["age"], 14 * 86400))
            if len(rows) < 30:
                continue
            first = [m for _, m in rows[:10] if m]
            start = sorted(first)[len(first) // 2] if first else None
            if not start:
                continue
            # least-squares slope, GB per day
            n = len(rows)
            mx = sum(t for t, _ in rows) / n
            my = sum(m for _, m in rows) / n
            den = sum((t - mx) ** 2 for t, _ in rows) or 1
            slope = sum((t - mx) * (m - my) for t, m in rows) / den * 86400
            growth = g["mem"] - start
            item = {"name": g["name"], "key": g["key"], "app": g["app"], "start": start, "now": g["mem"],
                    "peak": max(m for _, m in rows), "age": g["age"], "slope": slope, "growth": growth,
                    "series": [[t, m] for t, m in rows[:: max(1, n // 120)]], "protected": g["protected"]}
            item["flag"] = (not g["protected"] and growth >= SETTINGS.get("growth_gb", 2) * GB and g["mem"] >= 1.6 * start and slope > 0)
            res.append(item)
        res.sort(key=lambda x: -x["growth"])
        self.result = res[:12]
        for it in self.result:
            if it["flag"]:
                NOTIFIER.push("grow:" + it["name"], "%s keeps growing: %s" % (it["name"], fmt(it["now"])),
                              "Up from %s when it opened %s ago." % (fmt(it["start"]), dur_text(it["age"])),
                              {"label": "Restart %s" % it["name"], "act": {"kind": "restart_app", "app": it["app"]}},
                              cooldown=86400, setting="notify_growth")

    def flagged(self):
        return [x for x in self.result if x["flag"]]


GROWTH = GrowthTracker()


class Watcher:
    """Notices sudden drops in free space and finds what grew."""

    def __init__(self):
        self.samples = collections.deque(maxlen=400)
        self.events = collections.deque(maxlen=20)
        self.busy = False
        self.last = 0

    def tick(self, snap):
        now = time.time()
        free = snap["disk"]["free"]
        self.samples.append((now, free, (snap.get("mem") or {}).get("swap", 0)))
        if not SETTINGS.get("watch_on") or self.busy or now - self.last < 1800:
            return
        window = [x for x in self.samples if now - x[0] <= 600]
        if len(window) < 3:
            return
        top = max(window, key=lambda x: x[1])
        drop = top[1] - free
        if drop >= SETTINGS.get("watch_drop_gb", 5) * GB:
            self.last = now
            self.busy = True
            swap_growth = max(0, window[-1][2] - top[2])
            threading.Thread(target=self._investigate, args=(drop, now - top[0], swap_growth), daemon=True).start()

    def _investigate(self, drop, secs, swap_growth):
        try:
            found = []
            # 1) Spotlight: large files created or changed in the last 20 minutes (instant)
            o = out(["mdfind", "-onlyin", HOME, "kMDItemFSSize > 300000000 && kMDItemContentModificationDate >= $time.now(-1200)"], timeout=20)
            for pth in o.splitlines()[:30]:
                try:
                    st = os.stat(pth)
                    found.append({"p": dm.tilde(pth), "s": st.st_blocks * 512, "how": "new or changed file"})
                except OSError:
                    pass
            # 2) Folders that usually balloon, compared with the last scan
            data = Handler.disk.data if Handler.disk else None
            index = {}
            if data:
                def walk(n):
                    if n.get("d"):
                        index[n["p"]] = n["s"]
                        for c in n.get("c", []):
                            walk(c)
                walk(data["tree"])
            for rel in ("Downloads", "Library/Containers/com.docker.docker", "Library/Developer/Xcode/DerivedData",
                        "Library/Developer/CoreSimulator", ".ollama/models", "Library/Caches", ".Trash", "Movies", "Desktop",
                        ".cache/huggingface", "Library/Application Support/Cursor", "Library/Containers"):
                full = os.path.join(HOME, rel)
                if not os.path.isdir(full):
                    continue
                before = index.get("~/" + rel)
                if before is None:
                    continue
                now_s = dm.measure(full)
                if now_s - before >= 1 * GB:
                    found.append({"p": "~/" + rel, "s": now_s, "grew": now_s - before, "how": "folder grew since last scan"})
            found.sort(key=lambda f: -(f.get("grew") or f["s"]))
            ev = {"t": time.time(), "drop": drop, "secs": secs, "swap": swap_growth, "found": found[:8]}
            self.events.appendleft(ev)
            top = found[0] if found else None
            body = ("Biggest: %s (%s)" % (top["p"], fmt(top.get("grew") or top["s"]))) if top else \
                ("Swap grew by %s. Memory pressure is using disk." % fmt(swap_growth) if swap_growth > 1 * GB else
                 "Nothing obvious in your home folder; could be swap, snapshots or system files.")
            NOTIFIER.push("watch", "Free space dropped %s in %d min" % (fmt(drop), max(1, secs // 60)), body,
                          {"label": "Show in Finder", "act": {"kind": "reveal", "path": top["p"]}} if top else {"label": "Open Storage", "open": "storage"},
                          cooldown=0)
            HUB.send("watch", ev, remember=False)
        finally:
            self.busy = False


WATCHER = Watcher()


def digest_text(d):
    parts = []
    if d.get("freed"):
        parts.append("freed %s" % fmt(d["freed"]))
    if d.get("free_change") is not None:
        parts.append("free space %s%s" % ("+" if d["free_change"] >= 0 else "−", fmt(abs(d["free_change"]))))
    if d.get("top_mem"):
        parts.append("%s used the most memory (%s avg)" % (d["top_mem"][0]["name"], fmt(d["top_mem"][0]["mem"])))
    crashes = sum(v["count"] for k, v in d["events"].items() if k.startswith("crash"))
    if crashes:
        parts.append("%d crash%s" % (crashes, "es" if crashes > 1 else ""))
    if d.get("hot_minutes"):
        parts.append("ran hot for %d min" % d["hot_minutes"])
    if not parts:
        return "A quiet week."
    t = "; ".join(parts)
    return t[0].upper() + t[1:] + "."


def check_digest():
    if not SETTINGS.get("digest_on") or not HISTORY:
        return
    lt = time.localtime()
    if lt.tm_wday != int(SETTINGS.get("digest_day", 6)) or lt.tm_hour < int(SETTINGS.get("digest_hour", 18)):
        return
    if time.time() - float(SETTINGS.get("last_digest", 0)) < 5 * 86400:
        return
    SETTINGS["last_digest"] = time.time()
    save_settings()
    d = HISTORY.digest(7)
    NOTIFIER.push("digest", "Your week on this Mac", digest_text(d), {"label": "Open digest", "open": "home"}, cooldown=0, setting="digest_on")


class Focus:
    """Profiles that quit a set of apps (and optionally unload models / close heavy tabs), then bring them back."""

    @staticmethod
    def start(name, sampler):
        if (SETTINGS.get("focus_active") or {}).get("name"):
            Focus.end(sampler)
        prof = next((p for p in SETTINGS.get("profiles", []) if p.get("name") == name), None)
        if not prof:
            raise ValueError("No profile called %s." % name)
        with sampler.lock:
            groups = dict(sampler.groups)
        wanted = {n.lower() for n in prof.get("quit", [])}
        quit_paths, names = [], []
        for g in groups.values():
            if g["kind"] == "app" and not g["protected"] and g["name"].lower() in wanted and g["app"]:
                try:
                    act({"kind": "quit_app", "app": g["app"]}, sampler, None)
                    quit_paths.append(g["app"])
                    names.append(g["name"])
                except Exception as e:
                    print("focus quit %s: %s" % (g["name"], e), file=sys.stderr)
        extra = []
        if prof.get("unload_models"):
            for m in list(sampler.models):
                try:
                    act({"kind": "unload_model", "model": m["name"]}, sampler, None)
                    extra.append("unloaded " + m["name"])
                except Exception:
                    pass
        if prof.get("close_heavy_tabs"):
            for b, info in (sampler.tabs or {}).items():
                heavy = [{"win": t["win"], "id": t["id"], "url": t["url"]} for t in info.get("tabs", []) if t.get("heavy") or t.get("dup")]
                if heavy:
                    try:
                        n = close_tabs(b, heavy)
                        extra.append("closed %d tabs in %s" % (n, b))
                    except Exception:
                        pass
        if prof.get("shortcut_on"):
            sh(["shortcuts", "run", prof["shortcut_on"]], timeout=30)
        SETTINGS["focus_active"] = {"name": name, "icon": prof.get("icon", ""), "quit": quit_paths, "started": time.time(),
                                    "shortcut_off": prof.get("shortcut_off", "")}
        save_settings()
        if HISTORY:
            HISTORY.event("focus", "%s on: quit %s" % (name, ", ".join(names) or "nothing"))
        return "%s on. Quit %s%s." % (name, ", ".join(names) or "no apps", ("; " + "; ".join(extra)) if extra else "")

    @staticmethod
    def end(sampler):
        fa = SETTINGS.get("focus_active") or {}
        if not fa.get("name"):
            raise ValueError("No focus mode is on.")
        reopened = []
        for pth in fa.get("quit", []):
            if os.path.isdir(pth):
                subprocess.Popen(["open", "-g", "-a", pth])
                reopened.append(os.path.basename(pth)[:-4])
        if fa.get("shortcut_off"):
            sh(["shortcuts", "run", fa["shortcut_off"]], timeout=30)
        SETTINGS["focus_active"] = {}
        save_settings()
        if HISTORY:
            HISTORY.event("focus", "%s off" % fa["name"])
        return "%s off.%s" % (fa["name"], (" Reopened " + ", ".join(reopened) + ".") if reopened else "")


class Tasks:
    """Whitelisted longer-running maintenance commands, run one at a time with live output."""
    DEFS = {
        "brew_upgrade": ("Upgrade Homebrew packages", "brew upgrade"),
        "brew_cleanup": ("Clean Homebrew cache", "brew cleanup -s --prune=all"),
        "brew_update": ("Update Homebrew", "brew update"),
        "sim_unavailable": ("Delete unavailable simulators", "xcrun simctl delete unavailable"),
        "docker_prune": ("Prune unused Docker data", "docker system prune -f"),
        "flush_dns": ("Flush DNS cache", "dscacheutil -flushcache && killall -HUP mDNSResponder 2>/dev/null; echo done"),
        "qlmanage_reset": ("Reset Quick Look cache", "qlmanage -r cache"),
        "cursor_vacuum": ("Compact Cursor & VS Code databases",
                          "for app in Cursor Code; do f=\"$HOME/Library/Application Support/$app/User/globalStorage/state.vscdb\"; "
                          "[ -f \"$f\" ] || continue; if pgrep -xq \"$app\" || pgrep -fq \"/$app.app/\"; then echo \"Quit $app first, then run this again.\"; continue; fi; "
                          "b=$(du -k \"$f\" | cut -f1); echo \"→ $app: compacting $((b/1024)) MB…\"; sqlite3 \"$f\" 'VACUUM;' && "
                          "a=$(du -k \"$f\" | cut -f1) && echo \"  now $((a/1024)) MB (freed $(((b-a)/1024)) MB)\"; done"),
        "repos_fetch": ("Fetch all git repos", None),
    }

    def __init__(self):
        self.cur = None

    def start(self, name):
        if name not in self.DEFS:
            raise ValueError("Unknown task.")
        if self.cur and not self.cur.get("done"):
            raise ValueError("Another task is running: %s" % self.cur["title"])
        title, cmd = self.DEFS[name]
        self.cur = {"name": name, "title": title, "out": "", "done": False, "rc": None, "started": time.time()}
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()
        return "Started: %s" % title

    def _run(self, cmd):
        c = self.cur
        env = dict(os.environ, PATH=os.pathsep.join([os.environ.get("PATH", "")] + inv.PATHS), HOMEBREW_NO_ANALYTICS="1")
        if cmd is None:  # repos_fetch
            repos = (INV.data.get("repos") if INV else None) or []
            cmd = " ; ".join("echo '→ %s' && git -C %s fetch --all --prune -q" % (r["name"].replace("'", ""), shlex.quote(os.path.expanduser(r["path"])))
                             for r in repos[:80]) or "echo 'no repos found'"
        try:
            p = subprocess.Popen(["bash", "-lc", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            last = 0
            for line in p.stdout:
                c["out"] = (c["out"] + line)[-6000:]
                if time.time() - last > 0.5:
                    last = time.time()
                    HUB.send("task", c)
            c["rc"] = p.wait()
        except OSError as e:
            c["out"] += str(e)
            c["rc"] = -1
        c["done"] = True
        HUB.send("task", c)
        NOTIFIER.push("task:" + c["name"], "%s %s" % (c["title"], "finished" if c["rc"] == 0 else "failed"),
                      (c["out"].strip().splitlines() or [""])[-1][:120], {"label": "Show output", "open": "developer"},
                      cooldown=0, setting="notify_done")
        if INV and c["name"].startswith("brew"):
            INV.request(["brew"], force=True)
        if INV and c["name"] == "repos_fetch":
            INV.request(["repos"], force=True)


TASKS = Tasks()


# ---------------------------------------------------------------------------
# Live system sampling
# ---------------------------------------------------------------------------
class Sampler:
    def __init__(self, interval=2.0, demo=False):
        self.interval = interval
        self.demo = demo
        self.lock = threading.Lock()
        self.cpu_hist = collections.deque(maxlen=150)
        self.mem_hist = collections.deque(maxlen=150)
        self.gpu_hist = collections.deque(maxlen=150)
        self.app_cpu = collections.defaultdict(lambda: collections.deque(maxlen=15))
        self.groups = {}           # key -> group dict (latest)
        self.procs = {}            # pid -> proc dict (latest)
        self.tabs = {}             # browser -> {"status", "tabs": [...]}
        self.listeners = []
        self.models = []
        self.docker = None         # running container count, or None
        self.snapshot = None
        self.ncpu = os.cpu_count() or 1
        self.mem_total = int(sysctl("hw.memsize") or 0) if MAC else self._meminfo().get("MemTotal", 0)
        self._linux_prev = None
        self.wake = threading.Event()
        self.slow_wake = threading.Event()
        self.mac = None
        if MAC:
            try:
                self.mac = macstats.MacStats()
                if self.mac.errors:
                    print("  some sensors unavailable: %s" % ", ".join(sorted(self.mac.errors)), file=sys.stderr)
            except Exception as e:
                print("  hardware stats unavailable:", e, file=sys.stderr)
        self.slow_n = 0
        self.top_ok = False

    # ---- raw collectors -------------------------------------------------
    def _meminfo(self):
        d = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    d[k] = int(v.split()[0]) * 1024
        except OSError:
            pass
        return d

    def _ps(self):
        procs = {}
        txt = out(["ps", "-axo", "pid=,ppid=,user=,pcpu=,rss=,etime=,comm="], timeout=5)
        for line in txt.splitlines():
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            try:
                pid = int(parts[0])
                procs[pid] = {"pid": pid, "ppid": int(parts[1]), "user": parts[2], "cpu": float(parts[3]),
                              "mem": int(parts[4]) * 1024, "age": etime_secs(parts[5]), "path": parts[6].strip()}
            except ValueError:
                continue
        return procs

    def _top(self):
        """Two-sample top: accurate interval CPU per process, memory footprint, header stats."""
        txt = out(["top", "-l", "2", "-s", "1", "-n", "800", "-stats", "pid,cpu,mem,th,power"], timeout=8)
        blocks = txt.split("Processes:")
        if len(blocks) < 3:
            return None
        blk = blocks[-1]
        head = {"cpu": None, "load": None}
        m = re.match(r"\s*(\d+) total,.*?(\d+) running.*?(\d+) threads", blk)
        if m:
            head["nprocs"], head["running"], head["threads"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m = re.search(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", blk)
        if m:
            head["cpu"] = 100 - float(m.group(3))
            head["cpu_user"], head["cpu_sys"] = float(m.group(1)), float(m.group(2))
        m = re.search(r"Load Avg:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", blk)
        if m:
            head["load"] = [float(m.group(i)) for i in (1, 2, 3)]
        rows = {}
        col = None
        cap = 100.0 * self.ncpu
        for line in blk.splitlines():
            if line.lstrip().startswith("PID"):
                hdr = line.split()
                # map columns by header name: macOS versions differ in column order
                col = {"pid": hdr.index("PID")}
                for key, prefix in (("cpu", "%CPU"), ("mem", "MEM"), ("th", "#TH"), ("pw", "POWER")):
                    col[key] = next((i for i, h in enumerate(hdr) if h.startswith(prefix)), None)
                continue
            if not col:
                continue
            p = line.split()
            if len(p) < len(col):
                continue
            try:
                g = lambda k: p[col[k]] if col.get(k) is not None and col[k] < len(p) else None
                cpu = min(cap, float(g("cpu") or 0))
                mem = parse_size(g("mem") or "0")
                th = int((g("th") or "0").split("/")[0])
                pw = float(g("pw") or 0)
                rows[int(p[col["pid"]])] = (cpu, mem, th, pw)
            except ValueError:
                pass
        head["rows"] = rows
        return head

    def _vm(self):
        txt = out(["vm_stat"], timeout=3)
        m = re.search(r"page size of (\d+) bytes", txt)
        page = int(m.group(1)) if m else 16384
        v = {}
        for line in txt.splitlines()[1:]:
            if ":" in line:
                k, val = line.split(":", 1)
                try:
                    v[k.strip().strip('"')] = int(val.strip().rstrip(".")) * page
                except ValueError:
                    pass
        wired = v.get("Pages wired down", 0)
        comp = v.get("Pages occupied by compressor", 0)
        purge = v.get("Pages purgeable", 0)
        anon = v.get("Anonymous pages", v.get("Pages active", 0))
        filebk = v.get("File-backed pages", 0)
        app = max(0, anon - purge)
        counters = {k: v.get(n, 0) for k, n in (("pageins", "Pageins"), ("pageouts", "Pageouts"), ("swapins", "Swapins"),
                                                  ("swapouts", "Swapouts"), ("compressions", "Compressions"),
                                                  ("decompressions", "Decompressions"), ("faults", "Translation faults"))}
        return {"app": app, "wired": wired, "compressed": comp, "cached": filebk + purge, "counters": counters}

    def _gpu(self):
        txt = out(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=3)
        m = re.search(r'"Device Utilization %"=(\d+)', txt)
        mem = re.search(r'"In use system memory"=(\d+)', txt)
        return (int(m.group(1)) if m else None), (int(mem.group(1)) if mem else None)

    def _front(self):
        o = out(["sh", "-c", 'lsappinfo info -only bundlepath "$(lsappinfo front)"'], timeout=2)
        m = re.search(r'"LSBundlePath"="([^"]+)"', o)
        return m.group(1) if m else None

    # ---- one fast sample ------------------------------------------------
    def sample(self):
        procs = self._ps()
        head = self._top() if MAC else None
        self.top_ok = bool(head and head.get("rows"))
        if head:
            for pid, (cpu, mem, th, pw) in head["rows"].items():
                if pid in procs:
                    procs[pid].update(cpu=cpu, mem=mem, th=th, power=pw)
        cpu_total = head["cpu"] if head and head["cpu"] is not None else self._linux_cpu()
        load = head["load"] if head and head.get("load") else list(os.getloadavg())

        # memory
        if MAC:
            vm = self._vm()
            used = vm["app"] + vm["wired"] + vm["compressed"]
            plevel = int(sysctl("kern.memorystatus_vm_pressure_level") or 1)
            free_pct = int(sysctl("kern.memorystatus_level") or 0)
            sw = sysctl("vm.swapusage")
            m = re.search(r"used = ([\d.]+)M", sw)
            swap = float(m.group(1)) * 1024 * 1024 if m else 0
            gpu, gpu_mem = self._gpu()
            boot = re.search(r"sec = (\d+)", sysctl("kern.boottime"))
            uptime = time.time() - int(boot.group(1)) if boot else None
            front = self._front()
        else:
            mi = self._meminfo()
            used = mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)
            vm = {"app": used - mi.get("Shmem", 0), "wired": 0, "compressed": 0,
                  "cached": mi.get("Cached", 0) + mi.get("Buffers", 0)}
            free_pct = int(100 * mi.get("MemAvailable", 0) / max(1, mi.get("MemTotal", 1)))
            plevel = 1 if free_pct > 25 else (2 if free_pct > 10 else 4)
            swap = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
            gpu = gpu_mem = None
            try:
                uptime = float(open("/proc/uptime").read().split()[0])
            except OSError:
                uptime = None
            front = None
        pressure = {1: "normal", 2: "warning", 4: "critical"}.get(plevel, "normal")

        # group processes into apps
        groups = {}
        for p in procs.values():
            if p["pid"] == MYPID:
                continue
            path = p["path"]
            app = app_of(path)
            if app:
                key, name, kind = app, os.path.basename(app)[:-4], "app"
            else:
                name = os.path.basename(path) or path
                key, kind = "proc:" + name, "proc"
            mine = p["user"] in (ME_NAME, str(ME))
            sysish = (not mine) or path.startswith(("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/")) \
                or (app or "").startswith("/System/Library/")
            g = groups.get(key)
            if not g:
                g = groups[key] = {"key": key, "name": name, "kind": kind, "app": app, "cpu": 0.0, "mem": 0,
                                   "n": 0, "pids": [], "mine": mine, "system": sysish, "th": 0, "power": 0.0, "age": 0}
            g["cpu"] += p["cpu"]
            g["mem"] += p["mem"]
            g["th"] += p.get("th", 0)
            g["power"] += p.get("power", 0.0)
            g["n"] += 1
            g["pids"].append(p["pid"])
            g["age"] = max(g["age"], p.get("age", 0))
            g["mine"] = g["mine"] or mine
            g["system"] = g["system"] and sysish
        safari = next((g for g in groups.values() if g["kind"] == "app" and g["name"] == "Safari"), None)
        if safari:
            for k in [k for k, g in groups.items() if g["name"].startswith("com.apple.WebKit.") or g["name"] == "com.apple.Safari.SafeBrowsing.Service"]:
                w = groups.pop(k)
                safari["cpu"] += w["cpu"]
                safari["mem"] += w["mem"]
                safari["n"] += w["n"]
                safari["pids"] += w["pids"]
                safari["th"] += w["th"]
                safari["power"] += w["power"]
                safari["webkit"] = True
        for k, g in groups.items():
            g["cpu"] = min(g["cpu"], 100.0 * self.ncpu)
            h = self.app_cpu[k]
            h.append(g["cpu"])
            g["cpu_avg"] = sum(h) / len(h)
            g["samples"] = len(h)
            g["front"] = bool(front and g["app"] == front)
            g["protected"] = g["name"] in PROTECTED or g["system"] or not g["mine"]
            g["explain"] = SYSTEM_EXPLAIN.get(g["name"])
        for k in list(self.app_cpu):
            if k not in groups:
                del self.app_cpu[k]

        if cpu_total is None:
            cpu_total = min(100.0, sum(p["cpu"] for p in procs.values()) / self.ncpu)
        du = shutil.disk_usage(HOME)
        now = time.time()
        self.cpu_hist.append(round(cpu_total, 1))
        self.mem_hist.append(round(used / max(1, self.mem_total) * 100, 1))
        if gpu is not None:
            self.gpu_hist.append(gpu)

        with self.lock:
            self.groups, self.procs = groups, procs
        apps = sorted(groups.values(), key=lambda g: -g["mem"])
        snap = {
            "t": now,
            "cpu": {"total": round(cpu_total, 1), "user": head.get("cpu_user") if head else None,
                    "sys": head.get("cpu_sys") if head else None, "load": [round(x, 2) for x in load],
                    "cores": self.ncpu, "hist": list(self.cpu_hist)},
            "gpu": {"util": gpu, "mem": gpu_mem, "hist": list(self.gpu_hist)} if gpu is not None else None,
            "mem": {"total": self.mem_total, "used": used, "app": vm["app"], "wired": vm["wired"],
                    "compressed": vm["compressed"], "cached": vm["cached"], "swap": swap,
                    "pressure": pressure, "free_pct": free_pct, "hist": list(self.mem_hist)},
            "disk": {"total": du.total, "used": du.used, "free": du.free},
            "uptime": uptime,
            "procs": len(procs),
            "apps": [self._pub(g) for g in apps[:60]],
            "tabs": self.tabs,
            "listeners": self.listeners,
            "models": self.models,
            "docker": self.docker,
            "threads": head.get("threads") if head else None,
            "running": head.get("running") if head else None,
        }
        if self.mac:
            try:
                snap["hw"] = self.mac.fast(vm.get("counters"))
            except Exception as e:
                print("hw sample:", e, file=sys.stderr)
            sd = dict(self.mac.slow_data)
            if sd.get("nettop"):
                for n in sd["nettop"]:
                    pr = procs.get(n["pid"])
                    a = app_of(pr["path"]) if pr else None
                    n["app"] = os.path.basename(a)[:-4] if a else n["name"]
            snap["hw_slow"] = sd
            snap["sys"] = self.mac.static
        if self.demo:
            demo_extras(snap)
            groups = dict(groups)
            for a in snap["apps"]:
                groups.setdefault(a["key"], dict(a, samples=15, pids=[]))
        snap["suggestions"] = suggest(snap, groups)
        self.snapshot = snap
        if HISTORY and not self.demo:
            try:
                HISTORY.add(snap)
            except Exception as e:
                print("history:", e, file=sys.stderr)
        NOTIFIER.check(snap, groups)
        AUTOPILOT.tick(snap, self, groups)
        WATCHER.tick(snap)
        GROWTH.tick(self, groups)
        HUB.send("live", snap)

    @staticmethod
    def _pub(g):
        return {k: g[k] for k in ("key", "name", "kind", "app", "cpu", "cpu_avg", "mem", "n", "mine", "system",
                                  "front", "protected", "explain", "th", "power", "age")}

    def _linux_cpu(self):
        try:
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
        except OSError:
            return None
        idle, total = v[3] + v[4], sum(v)
        prev, self._linux_prev = self._linux_prev, (idle, total)
        if not prev or total == prev[1]:
            return 0.0
        return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))

    # ---- slower collectors (tabs, ports, models, docker) ---------------
    def slow(self):
        names = {g["name"] for g in self.groups.values() if g["kind"] == "app"}
        tabs = {}
        if MAC and (self.active() or SETTINGS.get("auto_tabs")):
            for b in BROWSERS:
                if b in names:
                    tabs[b] = read_tabs(b)
            self.tabs = tabs
        elif not self.active():
            pass  # keep the last list; refreshed when a page connects
        else:
            self.tabs = tabs
        self.listeners = read_listeners(self.procs)
        if self.mac:
            self.slow_n += 1
            which = {"net", "misc"} | ({"wifi"} if self.slow_n % 6 == 1 else set())
            try:
                self.mac.slow(which)
            except Exception as e:
                print("hw slow:", e, file=sys.stderr)
        self.models = read_models()
        if shutil.which("docker") and ("Docker" in names or "OrbStack" in names):
            o, _, rc = sh(["docker", "ps", "-q"], timeout=4)
            self.docker = len(o.split()) if rc == 0 else None
        else:
            self.docker = None

    def run(self):
        def slow_loop():
            while True:
                try:
                    self.slow()
                except Exception as e:  # never let a collector kill the server
                    print("slow collector:", e, file=sys.stderr)
                self.slow_wake.wait(10 if self.active() else 30)
                self.slow_wake.clear()
        threading.Thread(target=slow_loop, daemon=True).start()
        while True:
            t0 = time.time()
            try:
                self.sample()
            except Exception as e:
                print("sampler:", e, file=sys.stderr)
            iv = self.interval if self.active() else max(self.interval, float(SETTINGS.get("idle_interval", 10)))
            bat = ((self.snapshot or {}).get("hw") or {}).get("battery") or {}
            if bat.get("percent") is not None and not bat.get("plugged"):
                iv = max(iv, 4.0)  # gentler on battery
            self.wake.wait(max(0.2, iv - (time.time() - t0)))
            self.wake.clear()

    def active(self):
        """Someone is looking at the dashboard (an open page with a live connection)."""
        with HUB.lock:
            return bool(HUB.clients)

    def poke(self, slow=False):
        if slow:
            self.slow_wake.set()
        self.wake.set()


def read_tabs(browser):
    fs, rs = "character id 31", "character id 30"
    if browser == "Safari":
        script = '''
set out to ""
tell application "Safari"
  repeat with w in windows
    set wid to id of w
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      set u to ""
      set n to ""
      try
        set u to URL of t
      end try
      try
        set n to name of t
      end try
      if u is missing value then set u to ""
      set out to out & wid & (%s) & i & (%s) & u & (%s) & n & (%s)
    end repeat
  end repeat
end tell
return out''' % (fs, fs, fs, rs)
    else:
        script = '''
set out to ""
tell application %s
  repeat with w in windows
    set wid to id of w
    repeat with t in tabs of w
      set out to out & wid & (%s) & (id of t) & (%s) & (URL of t) & (%s) & (title of t) & (%s)
    end repeat
  end repeat
end tell
return out''' % (as_quote(browser), fs, fs, fs, rs)
    o, err, rc = osascript(script)
    if rc != 0:
        status = "denied" if ("-1743" in err or "Not authorized" in err) else "error"
        return {"status": status, "tabs": [], "error": err.strip()[-200:]}
    tabs, seen = [], collections.Counter()
    for rec in o.rstrip("\n").split("\x1e"):
        f = rec.split("\x1f")
        if len(f) < 4:
            continue
        url = f[2].strip()
        t = {"win": f[0].strip(), "id": f[1].strip(), "url": url, "title": f[3].strip() or url,
             "domain": domain_of(url)}
        t["heavy"] = any(t["domain"] == h or t["domain"].endswith("." + h) or h in t["domain"] for h in HEAVY_SITES)
        seen[url] += 1
        t["dup"] = seen[url] > 1 and bool(url) and not url.startswith(("chrome://newtab", "about:blank", "favorites://"))
        tabs.append(t)
    return {"status": "ok", "tabs": tabs[:400], "count": len(tabs)}


def close_tabs(browser, tabs):
    """tabs: list of {win, id, url}. Returns number closed."""
    if browser not in BROWSERS:
        raise ValueError("unknown browser")
    lines = ['tell application %s' % as_quote(browser), "set n to 0"]
    for t in tabs:
        win, tid, url = str(t["win"]), str(t["id"]), t.get("url", "")
        if not win.isdigit():
            continue
        if browser == "Safari":
            if not tid.isdigit():
                continue
            lines += ["try", "set t to tab %s of window id %s" % (tid, win),
                      "if (URL of t) is %s then" % as_quote(url), "close t", "set n to n + 1", "end if", "end try"]
        else:
            ref = tid if tid.isdigit() else as_quote(tid)
            lines += ["try", "close (every tab of window id %s whose id is %s)" % (win, ref), "set n to n + 1", "end try"]
    lines += ["end tell", "return n"]
    # Safari indexes shift as tabs close: close from the highest index down
    if browser == "Safari":
        pass
    o, err, rc = osascript("\n".join(lines), timeout=15)
    if rc != 0:
        raise RuntimeError(err.strip() or "AppleScript failed")
    return int(o.strip() or 0)


def read_listeners(procs):
    if not shutil.which("lsof"):
        return []
    o = out(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"], timeout=6)
    found, pid, cmd = {}, None, None
    for line in o.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid = int(val)
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid:
            port = val.rsplit(":", 1)[-1]
            if not port.isdigit():
                continue
            host = val.rsplit(":", 1)[0]
            ent = found.setdefault(pid, {"pid": pid, "cmd": cmd, "ports": set(), "local": True})
            ent["ports"].add(int(port))
            if host in ("*", "0.0.0.0", "[::]"):
                ent["local"] = False
    found.pop(MYPID, None)
    if found:
        o = out(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, found)), "-Fpn"], timeout=6)
        pid = None
        for line in o.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid in found:
                found[pid]["cwd"] = dm.tilde(line[1:])
    res = []
    for pid, e in found.items():
        p = procs.get(pid, {})
        path = p.get("path", "")
        app = app_of(path)
        name = os.path.basename(app)[:-4] if app else e["cmd"]
        res.append({"pid": pid, "cmd": e["cmd"], "name": name, "ports": sorted(e["ports"]), "cwd": e.get("cwd", ""),
                    "mem": p.get("mem", 0), "cpu": p.get("cpu", 0.0), "app": bool(app),
                    "dev": any((e["cmd"] or "").startswith(r) for r in DEV_RUNTIMES)
                    or (not app and e.get("cwd", "").startswith("~/")),
                    "lan": not e["local"]})
    res.sort(key=lambda r: (not r["dev"], r["ports"][0]))
    return res


def read_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=0.6) as r:
            d = json.load(r)
    except Exception:
        return []
    return [{"name": m.get("name"), "size": m.get("size", 0), "vram": m.get("size_vram", 0),
             "expires": m.get("expires_at", "")} for m in d.get("models", [])]


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------
SEV = {"high": 0, "med": 1, "low": 2, "info": 3}


def fmt(b):
    return ("%.1f GB" % (b / GB)) if b >= GB else ("%d MB" % (b / MB))


def suggest(s, groups):
    out_, mem = [], s["mem"]
    tight = mem["pressure"] != "normal" or mem["swap"] > 4 * GB or mem["free_pct"] < 20

    def add(id_, sev, kind, title, sub, actions=(), group=None):
        out_.append({"id": id_, "sev": sev, "kind": kind, "title": title, "sub": sub,
                     "actions": list(actions), "group": group})

    if mem["pressure"] != "normal":
        add("mem-pressure", "high", "mem", "Memory pressure is %s" % mem["pressure"],
            "%s of swap in use, so things will feel slow. Quitting the apps below frees memory straight away."
            % fmt(mem["swap"]))
    elif mem["swap"] > 4 * GB:
        add("swap", "med", "mem", "%s of swap in use" % fmt(mem["swap"]),
            "Your Mac has been short on memory recently. Quitting idle apps helps; a restart clears swap completely.")

    # Idle apps holding a lot of memory
    big = max(1.5 * GB, 0.06 * (mem["total"] or 16 * GB))
    for g in sorted(groups.values(), key=lambda g: -g["mem"]):
        if g["kind"] != "app" or g["protected"] or g["front"] or g["name"] in BROWSERS or g["name"] in ("Docker", "OrbStack"):
            continue
        if g["mem"] >= big and g["cpu_avg"] < 3 and g["samples"] >= 5:
            add("quit:" + g["key"], "med" if tight else "low", "app", "Quit %s" % g["name"],
                "Using %s while idle%s." % (fmt(g["mem"]), " across %d processes" % g["n"] if g["n"] > 3 else ""),
                [{"label": "Quit", "kind": "quit_app", "app": g["app"], "confirm": "Quit %s?" % g["name"]}],
                group=g["key"])

    # CPU hogs (sustained over ~30 s)
    for g in sorted(groups.values(), key=lambda g: -g["cpu_avg"]):
        if g["samples"] < 8 or g["cpu_avg"] < 70:
            break
        if g["explain"]:
            add("cpu:" + g["key"], "info", "cpu", "%s is busy (%d%% CPU)" % (g["name"], g["cpu_avg"]), g["explain"])
        elif g["protected"]:
            continue
        else:
            acts = [{"label": "Quit", "kind": "quit_app", "app": g["app"], "confirm": "Quit %s?" % g["name"]}] if g["app"] \
                else [{"label": "Stop", "kind": "stop_group", "key": g["key"], "confirm": "Send stop signal to %s?" % g["name"]}]
            add("cpu:" + g["key"], "high" if g["cpu_avg"] > 150 else "med", "cpu",
                "%s is using %d%% CPU" % (g["name"], g["cpu_avg"]),
                "Sustained for the last 30 seconds%s." % (" — it's the app you're using" if g["front"] else ""),
                acts, group=g["key"])

    # Browser tabs
    for b, info in (s.get("tabs") or {}).items():
        g = groups.get("/Applications/%s.app" % b) or next((x for x in groups.values() if x["name"] == b), None)
        bmem = g["mem"] if g else 0
        if info.get("status") == "denied":
            add("tabs-perm:" + b, "info", "tabs", "Let diskmap see your %s tabs" % b,
                "Allow your terminal app under System Settings › Privacy & Security › Automation › %s." % b)
            continue
        tabs = info.get("tabs", [])
        dups = [t for t in tabs if t["dup"]]
        if len(dups) >= 2:
            add("dups:" + b, "low", "tabs", "Close %d duplicate tabs in %s" % (len(dups), b),
                "Same page open more than once. Keeps the first copy of each.",
                [{"label": "Close duplicates", "kind": "close_tabs", "browser": b,
                  "tabs": [{"win": t["win"], "id": t["id"], "url": t["url"]} for t in dups]}])
        heavy = [t for t in tabs if t["heavy"] and not t["dup"]]
        if len(tabs) >= 25 or (bmem > 4 * GB and heavy):
            doms = collections.Counter(t["domain"] for t in heavy)
            top = ", ".join("%s ×%d" % (d, n) if n > 1 else d for d, n in doms.most_common(4))
            add("tabs:" + b, "med" if tight and bmem > 3 * GB else "low", "tabs",
                "%s: %d tabs using %s" % (b, len(tabs), fmt(bmem)),
                ("Heaviest kinds of pages open: %s." % top) if top else "Closing tabs you're not using frees memory.",
                [{"label": "Review tabs", "kind": "scroll", "target": "tabs"}], group=g["key"] if g else None)

    # Ollama models resident in memory
    for m in s.get("models") or []:
        add("ollama:" + m["name"], "med" if tight else "low", "ai", "Unload %s" % m["name"],
            "Ollama is keeping %s in memory. It reloads automatically next time you use it." % fmt(m["size"]),
            [{"label": "Unload", "kind": "unload_model", "model": m["name"]}])

    # Docker with nothing running
    dg = next((x for x in groups.values() if x["name"] in ("Docker", "OrbStack")), None)
    vm = groups.get("proc:com.docker.virtualization") or groups.get("proc:com.docker.hyperkit")
    dmem = (dg["mem"] if dg else 0) + (vm["mem"] if vm else 0)
    if dg and s.get("docker") == 0 and dmem > 1 * GB:
        add("docker-idle", "med" if tight else "low", "app", "Quit %s" % dg["name"],
            "No containers are running, but its VM is holding %s." % fmt(dmem),
            [{"label": "Quit", "kind": "quit_app", "app": dg["app"], "confirm": "Quit %s?" % dg["name"]}], group=dg["key"])

    # Emulators
    for key, label in (("proc:qemu-system-aarch64", "Android emulator"), ("proc:qemu-system-x86_64", "Android emulator")):
        g = groups.get(key)
        if g and g["mem"] > 1 * GB and g["cpu_avg"] < 15:
            add("emu:" + key, "low", "app", "Close the %s" % label, "Idle, holding %s." % fmt(g["mem"]),
                [{"label": "Stop", "kind": "stop_group", "key": key, "confirm": "Stop the emulator?"}], group=key)

    # Dev servers
    devs = [l for l in s.get("listeners") or [] if l["dev"]]
    if len(devs) >= 3:
        add("devs", "low", "dev", "%d dev servers running" % len(devs),
            "Ports %s. Stop the ones you're done with." % ", ".join(str(l["ports"][0]) for l in devs[:6]),
            [{"label": "Show", "kind": "scroll", "target": "dev"}])
    for l in devs:
        if l["lan"]:
            add("lan:%d" % l["pid"], "info", "dev", "%s on :%d is reachable from your network" % (l["cmd"], l["ports"][0]),
                "It's listening on all interfaces, so other devices on this Wi-Fi can open it.",
                [{"label": "Stop", "kind": "stop_pid", "pid": l["pid"], "confirm": "Stop %s?" % l["cmd"]}])

    # Hardware: heat, fans, sleep, battery, network
    hw, hs = s.get("hw") or {}, s.get("hw_slow") or {}
    t = (hw.get("temps") or {}).get("cpu_max")
    therm = hw.get("thermal")
    if (t and t >= 95) or therm in ("heavy", "trapping", "sleeping"):
        hog = max((g for g in groups.values() if g["mine"] and not g["protected"]), key=lambda g: g["cpu_avg"], default=None)
        acts = []
        if hog and hog["cpu_avg"] > 40 and hog["app"]:
            acts = [{"label": "Quit %s" % hog["name"], "kind": "quit_app", "app": hog["app"], "confirm": "Quit %s?" % hog["name"]}]
        add("heat", "high" if therm in ("heavy", "trapping", "sleeping") else "med", "heat",
            "Your Mac is running hot%s" % (" (%d °C)" % t if t else ""),
            "Thermal pressure is %s, so macOS may slow the CPU down.%s" % (therm or "elevated",
            " %s is the busiest app (%d%% CPU)." % (hog["name"], hog["cpu_avg"]) if hog and hog["cpu_avg"] > 40 else ""), acts)
    for f in hw.get("fans") or []:
        if f["max"] and f["rpm"] > 0.85 * f["max"]:
            add("fan%d" % f["id"], "info", "heat", "Fan %d is near full speed (%d rpm)" % (f["id"] + 1, f["rpm"]),
                "Something is working the chip hard. Check the CPU and GPU usage above.")
    for b in hs.get("sleep_blockers") or []:
        g = next((x for x in groups.values() if b["pid"] in x.get("pids", [])), None)
        if b["proc"] in ("powerd", "coreaudiod", "useractivityd", "sharingd", "WindowServer", "bluetoothd") and b["type"] != "PreventSystemSleep":
            continue
        acts = [{"label": "Quit", "kind": "quit_app", "app": g["app"], "confirm": "Quit %s?" % g["name"]}] \
            if g and g["app"] and not g["protected"] else []
        add("sleep:%d" % b["pid"], "info", "power", "%s is keeping your Mac awake" % (g["name"] if g else b["proc"]),
            "%s for %s%s." % ("Blocking display sleep" if "Display" in b["type"] else "Blocking sleep", b["age"],
                             ' — "%s"' % b["reason"] if b["reason"] else ""), acts)
    bat = hw.get("battery") or {}
    if bat.get("percent") is not None and not bat.get("plugged") and bat["percent"] <= 15:
        add("bat-low", "med", "power", "Battery at %d%%" % bat["percent"],
            "Plug in soon.%s" % (" About %d min left." % bat["time_min"] if bat.get("time_min") else ""))
    if bat.get("health") and bat["health"] < 80:
        add("bat-health", "info", "power", "Battery health is %d%%" % bat["health"],
            "%s cycles. Below 80%% Apple recommends a battery service." % bat.get("cycles"))
    for n in (hs.get("nettop") or [])[:3]:
        if n["down"] > 8 * MB or n["up"] > 4 * MB:
            add("net:%s" % n.get("app", n["name"]), "info", "net",
                "%s is using a lot of network" % n.get("app", n["name"]),
                "↓ %s/s  ↑ %s/s right now." % (fmt(n["down"]), fmt(n["up"])))
    wf = hs.get("wifi") or {}
    if wf.get("signal") and wf["signal"] < -75:
        add("wifi-weak", "info", "net", "Weak Wi-Fi signal (%d dBm)" % wf["signal"],
            "Moving closer to the router, or using 5/6 GHz, should speed things up.")
    if hs.get("ping_internet") and hs["ping_internet"] > 150:
        add("ping", "info", "net", "Internet latency is high (%d ms)" % hs["ping_internet"],
            "Router ping is %s." % ("%d ms" % hs["ping_gateway"] if hs.get("ping_gateway") else "unavailable"))
    pg = hw.get("paging") or {}
    if pg.get("swapouts", 0) > 5 * MB:
        add("thrash", "med", "mem", "Your Mac is swapping to disk",
            "Writing %s/s of memory to swap. Quit something big from the Apps list." % fmt(pg["swapouts"]))

    # Apps whose memory keeps growing since launch
    for g in GROWTH.flagged():
        add("grow:" + g["name"], "med" if tight else "low", "mem", "Restart %s" % g["name"],
            "It has grown from %s to %s since it opened %s ago. A restart gives the memory back." %
            (fmt(g["start"]), fmt(g["now"]), dur_text(g["age"])),
            [{"label": "Restart", "kind": "restart_app", "app": g["app"], "confirm": "Restart %s?" % g["name"]}], group=g["key"])
    fa = SETTINGS.get("focus_active") or {}
    if fa.get("name"):
        add("focus", "info", "focus", "%s mode is on" % fa["name"],
            "Started %s ago. %d app%s will reopen when you end it." % (dur_text(time.time() - fa.get("started", time.time())),
                                                                       len(fa.get("quit", [])), "" if len(fa.get("quit", [])) == 1 else "s"),
            [{"label": "End %s" % fa["name"], "kind": "focus_end"}])

    # Disk
    d = s["disk"]
    if d["free"] < 0.1 * d["total"]:
        add("disk-low", "high" if d["free"] < 0.05 * d["total"] else "med", "disk",
            "Disk is %d%% full" % (100 * d["used"] / d["total"]),
            "%s free. macOS slows down and can't update when the disk is this full." % fmt(d["free"]),
            [{"label": "Free up space", "kind": "scroll", "target": "disk"}])

    if s.get("uptime") and s["uptime"] > 21 * 86400 and mem["swap"] > 2 * GB:
        add("restart", "info", "mem", "Restart when convenient",
            "Up %d days with %s of swap. A restart resets memory and swap." % (s["uptime"] // 86400, fmt(mem["swap"])))

    out_.sort(key=lambda x: SEV[x["sev"]])
    return out_[:18]


def demo_extras(snap):
    """--demo: fake browser tabs, dev servers and models so the UI can be previewed off-Mac."""
    snap["apps"][:0] = [
        {"key": "/Applications/Google Chrome.app", "name": "Google Chrome", "kind": "app", "app": "/Applications/Google Chrome.app",
         "cpu": 18.2, "cpu_avg": 16.0, "mem": 6.4 * GB, "n": 41, "mine": True, "system": False, "front": True, "protected": False, "explain": None},
        {"key": "/Applications/Slack.app", "name": "Slack", "kind": "app", "app": "/Applications/Slack.app", "cpu": 0.4,
         "cpu_avg": 0.6, "mem": 2.1 * GB, "n": 6, "mine": True, "system": False, "front": False, "protected": False, "explain": None},
        {"key": "/Applications/Xcode.app", "name": "Xcode", "kind": "app", "app": "/Applications/Xcode.app", "cpu": 132.0,
         "cpu_avg": 121.0, "mem": 3.8 * GB, "n": 9, "mine": True, "system": False, "front": False, "protected": False, "explain": None},
        {"key": "proc:mds_stores", "name": "mds_stores", "kind": "proc", "app": None, "cpu": 88.0, "cpu_avg": 84.0,
         "mem": 0.31 * GB, "n": 1, "mine": False, "system": True, "front": False, "protected": True, "explain": SYSTEM_EXPLAIN["mds_stores"]},
    ]
    snap["gpu"] = snap["gpu"] or {"util": 37, "mem": int(5.1 * GB), "hist": [min(99, 20 + (i * 7) % 40) for i in range(len(snap["cpu"]["hist"]))]}
    snap["mem"].update({"total": 36 * GB, "used": 31.2 * GB, "app": 19.8 * GB, "wired": 3.9 * GB, "compressed": 7.5 * GB,
                        "cached": 3.6 * GB, "swap": 5.2 * GB, "pressure": "warning", "free_pct": 17})
    t = [("1", "11", "https://www.youtube.com/watch?v=abc", "Lo-fi beats — YouTube"),
         ("1", "12", "https://www.figma.com/design/xyz/App", "App design — Figma"),
         ("1", "13", "https://github.com/anthropics/claude-code", "anthropics/claude-code"),
         ("1", "14", "https://github.com/anthropics/claude-code", "anthropics/claude-code"),
         ("2", "21", "https://en.wikipedia.org/wiki/Skyline", "Skyline — Wikipedia"),
         ("2", "22", "https://docs.google.com/document/d/1", "Project plan — Google Docs"),
         ("2", "23", "https://www.youtube.com/watch?v=abc", "Lo-fi beats — YouTube"),
         ("2", "24", "https://news.ycombinator.com/", "Hacker News")]
    seen, tabs = collections.Counter(), []
    for w, i, u, ti in t:
        seen[u] += 1
        d = domain_of(u)
        tabs.append({"win": w, "id": i, "url": u, "title": ti, "domain": d, "dup": seen[u] > 1,
                     "heavy": any(h in d for h in HEAVY_SITES)})
    snap["tabs"] = {"Google Chrome": {"status": "ok", "tabs": tabs * 4, "count": 32}, "Safari": {"status": "denied", "tabs": []}}
    snap["listeners"] = [
        {"pid": 4242, "cmd": "node", "name": "node", "ports": [3000], "cwd": "~/projects/website", "mem": 0.9 * GB, "cpu": 1.2, "app": False, "dev": True, "lan": True},
        {"pid": 4243, "cmd": "node", "name": "node", "ports": [5173], "cwd": "~/projects/mobile-app", "mem": 0.4 * GB, "cpu": 0.1, "app": False, "dev": True, "lan": False},
        {"pid": 4244, "cmd": "python3.12", "name": "python3.12", "ports": [8000], "cwd": "~/projects/api", "mem": 0.2 * GB, "cpu": 0.0, "app": False, "dev": True, "lan": False},
        {"pid": 4245, "cmd": "ollama", "name": "ollama", "ports": [11434], "cwd": "~", "mem": 0.1 * GB, "cpu": 0.0, "app": False, "dev": True, "lan": False}]
    snap["models"] = [{"name": "deepseek-coder:6.7b", "size": int(8.3 * GB), "vram": int(8.3 * GB), "expires": ""}]
    snap["docker"] = 0
    snap["uptime"] = 26 * 86400
    import math, random
    n = len(snap["cpu"]["hist"])
    wave = lambda base, amp, k=0.3: [round(base + amp * math.sin(i * k) + random.random() * amp * 0.3, 2) for i in range(n)]
    sensors = [("PMU tdie%d" % i, 68 + i * 1.7) for i in range(1, 9)] + [("PMU tdev%d" % i, 52 + i) for i in range(1, 5)] + \
        [("GPU MTR Temp Sensor%d" % i, 61 + i * 2) for i in range(1, 4)] + [("NAND CH0 temp", 41.0), ("gas gauge battery", 33.5), ("PMU tcal", 51.0)]
    snap["hw"] = {
        "temps": {"sensors": [{"name": k, "c": v, "group": macstats.classify_sensor(k)} for k, v in sensors],
                  "cpu_max": 81.6, "cpu_avg": 74.0, "gpu": 67.0, "ssd": 41.0, "battery": 33.5, "hist": wave(74, 6)},
        "thermal": "nominal",
        "fans": [{"id": 0, "rpm": 2410, "min": 1200, "max": 5800}, {"id": 1, "rpm": 2290, "min": 1200, "max": 5800}],
        "smc_power": {"system": 38.4},
        "power": {"cpu": 14.2, "gpu": 9.8, "ane": 0.0, "dram": 1.9, "gpu_active": 37,
                  "channels": {"ECPU0": 0.4, "PCPU0": 6.1, "PCPU1": 7.7, "GPU Energy": 9.8, "DRAM": 1.9, "ISP": 0.02}},
        "cores": [{"i": i, "k": "E" if i < 4 else "P", "u": round(abs(math.sin(i)) * 60, 1), "s": round(abs(math.cos(i)) * 15, 1)} for i in range(16)],
        "battery": {"percent": 82, "charging": True, "plugged": True, "full": False, "cycles": 143, "health": 96.4,
                    "design_mah": 6249, "max_mah": 6024, "temp": 33.5, "voltage": 12.71, "amps": 1.8, "watts": 22.9,
                    "time_min": None, "adapter_w": 140, "adapter_name": "140W USB-C Power Adapter",
                    "system_load_w": 38.4, "power_in_w": 61.3, "condition": "Normal"},
        "sys_power": 38.4, "power_hist": wave(32, 9),
        "net": {"down": 2.4e6, "up": 1.9e5, "hist_down": wave(1.5e6, 1.2e6, .5), "hist_up": wave(2e5, 1.5e5, .4),
                "ifaces": {"en0": {"down": 2.4e6, "up": 1.9e5, "total_down": 48e9, "total_up": 6.1e9},
                           "utun4": {"down": 1.2e4, "up": 3e3, "total_down": 2e8, "total_up": 9e7}}},
        "io": {"read": 31e6, "write": 4.2e6, "riops": 820, "wiops": 140, "total_read": 2.1e12, "total_write": 1.4e12,
               "hist_r": wave(2e7, 1.5e7, .6), "hist_w": wave(4e6, 3e6, .45)},
        "paging": {"pageins": 1.1e6, "pageouts": 0, "swapins": 2e5, "swapouts": 0, "compressions": 8e6, "decompressions": 6e6, "faults": 4e8},
    }
    snap["hw_slow"] = {
        "gateway": "192.168.1.1", "ping_internet": 13.2, "ping_gateway": 2.1, "ping_hist": wave(14, 4),
        "ifaces": {"en0": {"if": "en0", "name": "Wi-Fi", "ip": "192.168.1.42"}, "utun4": {"if": "utun4", "name": "VPN", "ip": "100.64.0.7"}},
        "nettop": [{"name": "Google Chrome H", "pid": 1, "down": 1.9e6, "up": 4e4, "app": "Google Chrome"},
                   {"name": "Dropbox", "pid": 2, "down": 3.1e5, "up": 1.2e5, "app": "Dropbox"},
                   {"name": "ollama", "pid": 3, "down": 1.4e5, "up": 2e3, "app": "ollama"}],
        "bt": [{"name": "AirPods Pro", "percent": 64}, {"name": "Magic Mouse", "percent": 18}, {"name": "Magic Keyboard", "percent": 91}],
        "volumes": [{"mount": "/", "name": "Macintosh HD", "dev": "/dev/disk3s1s1", "total": 994.7e9, "used": 958.4e9, "free": 36.3e9},
                    {"mount": "/Volumes/T7", "name": "T7", "dev": "/dev/disk5s1", "total": 1e12, "used": 412e9, "free": 588e9}],
        "tm": {"running": False, "phase": None, "percent": None},
        "sleep_blockers": [{"pid": 4242, "proc": "Google Chrome", "age": "00:41:10", "type": "PreventUserIdleDisplaySleep", "reason": "Playing video"}],
        "usb": ["T7", "USB-C Digital AV Multiport Adapter", "Arduino Mega 2560"],
        "wifi": {"ssid": "Home-6E", "channel": "37 (6GHz, 160MHz)", "phy": "802.11ax", "rate": 1921, "security": "wpa3 personal",
                 "signal": -54, "noise": -94, "country": "US", "if": "en0"},
    }
    snap["sys"] = {"model": "Mac16,6", "chip": "Apple M4 Max", "os": "macOS 26.1 (25B78)", "cores_p": 12, "cores_e": 4, "cores": 16,
                   "mem": 36 * GB, "boot": time.time() - 26 * 86400, "smart": "Verified", "host": "demo-mac", "kernel": "25.1.0",
                   "gpu": {"name": "Apple M4 Max", "cores": "32"},
                   "displays": [{"name": "Color LCD", "res": "3456 x 2234 Retina", "main": True, "builtin": True},
                                {"name": "LG UltraFine", "res": "5120 x 2880 (5K)", "main": False, "builtin": False}]}
    snap["threads"], snap["running"] = 4120, 6
    for a in snap["apps"]:
        a.setdefault("th", 0); a.setdefault("power", 0.0)
        if a["name"] == "Google Chrome": a.update(th=812, power=24.1)
        if a["name"] == "Xcode": a.update(th=96, power=118.0)
    snap["apps"].insert(3, {"key": "/Applications/Docker.app", "name": "Docker", "kind": "app", "app": "/Applications/Docker.app",
                            "cpu": 0.8, "cpu_avg": 0.9, "mem": 4.2 * GB, "n": 5, "mine": True, "system": False, "front": False, "protected": False, "explain": None})


# ---------------------------------------------------------------------------
# Disk state: cached scan, background rescans, live re-measuring, cleanup
# ---------------------------------------------------------------------------
class Disk:
    def __init__(self, rescan_min, idle_days):
        self.lock = threading.Lock()
        self.data = None
        self.rev = 0
        self.rescan_min = rescan_min
        self.idle_days = idle_days
        self.scanning = None      # {"files", "path", "started"} while a scan runs
        self.cleaning = None
        self.last_scan = 0
        try:
            with open(CACHE) as f:
                self.data = json.load(f)
            self.last_scan = os.path.getmtime(CACHE)
            self._bump()
        except (OSError, ValueError):
            pass

    def _bump(self):
        self.rev += 1
        if self.data:
            self.data["rev"] = self.rev
            self.data["scan_id"] = "%d-%d" % (int(self.last_scan), self.rev)

    def status(self):
        return {"scanning": self.scanning, "cleaning": self.cleaning, "last_scan": self.last_scan,
                "rev": self.rev, "rescan_min": self.rescan_min, "has_data": self.data is not None}

    def start_scan(self):
        with self.lock:
            if self.scanning:
                return False
            self.scanning = {"files": 0, "path": "~", "started": time.time()}
        threading.Thread(target=self._scan, daemon=True).start()
        return True

    def _scan(self):
        last = [0]

        def progress(files, path):
            self.scanning.update(files=files, path=path)
            if time.time() - last[0] > 0.5:
                last[0] = time.time()
                HUB.send("scan", self.status())
        HUB.send("scan", self.status())
        try:
            data = dm.run_scan(HOME, self.idle_days, progress=progress, quiet=True)
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            tmp = CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, CACHE)
            with self.lock:
                self.data, self.last_scan = data, time.time()
                self._bump()
            safe = sum(r["s"] for r in data["recs"] if r["tier"] == "safe")
            if safe > 5 * GB:
                NOTIFIER.push("scan", "Disk scan done: %s can be cleaned safely" % fmt(safe), "Caches and build output that rebuild on their own.",
                              {"label": "Review", "open": "storage"}, cooldown=6 * 3600, setting="notify_done")
        except Exception as e:
            print("scan failed:", e, file=sys.stderr)
        finally:
            self.scanning = None
        HUB.send("scan", self.status())
        HUB.send("disk", {"rev": self.rev})

    def remeasure(self, only=None):
        """Re-measure the cleanup candidates so the page reflects changes without a full rescan."""
        data = self.data
        if not data or self.scanning:
            return
        index = {}

        def walk(n):
            if n.get("d"):
                index[n["p"]] = n
                for c in n.get("c", []):
                    walk(c)
        walk(data["tree"])
        changed = False
        for r in data["recs"]:
            if r["id"] == "ollama" or (only and r["id"] not in only):
                continue
            for it in r["items"]:
                new = dm.measure(os.path.expanduser(it["p"]))
                delta = new - it["s"]
                if abs(delta) < 1 * MB:
                    continue
                changed = True
                it["s"] = new
                # carry the change up the tree so the treemap stays honest
                p = it["p"]
                node = index.get(p)
                if node:
                    node["s"] = max(0, node["s"] + delta)
                while True:
                    parent = os.path.dirname(p)
                    if parent == p:
                        break
                    p = parent
                    if p in index:
                        index[p]["s"] = max(0, index[p]["s"] + delta)
                    if p == "~":
                        break
                data["tree"]["s"] = index.get("~", data["tree"])["s"]
            r["items"] = [i for i in r["items"] if i["s"] >= 1 * MB]
            r["s"] = sum(i["s"] for i in r["items"])
        data["recs"] = [r for r in data["recs"] if r["items"]]
        if changed:
            data["scanned"] = data["tree"]["s"]
            with self.lock:
                self._bump()
            HUB.send("disk", {"rev": self.rev})

    def clean(self, picks, scan_id):
        data = self.data
        if not data or str(data.get("scan_id")) != str(scan_id):
            raise ValueError("The scan changed — reload the page and try again.")
        if self.cleaning:
            raise ValueError("A cleanup is already running.")
        recs = {r["id"]: r for r in data["recs"]}
        plan = collections.OrderedDict()
        for rid, idx in picks:
            r = recs.get(rid)
            if not r or not (0 <= int(idx) < len(r["items"])):
                raise ValueError("Unknown item %s/%s" % (rid, idx))
            plan.setdefault(rid, []).append(int(idx))
        steps = []
        for rid, idxs in plan.items():
            r = recs[rid]
            cmds = list(r.get("pre", []))
            for i in sorted(set(idxs)):
                cmds += r["items"][i]["cmds"]
            cmds += r.get("post", [])
            cmds = [c for c in cmds if c.strip() and not c.lstrip().startswith("#")]
            if cmds:
                steps.append((r["title"], cmds))
        self.cleaning = {"total": len(steps), "done": 0, "title": "", "errors": [], "freed": 0}
        threading.Thread(target=self._clean, args=(steps, list(plan)), daemon=True).start()

    def _clean(self, steps, rec_ids):
        before = shutil.disk_usage(HOME).free
        for title, cmds in steps:
            self.cleaning["title"] = title
            HUB.send("clean", self.cleaning)
            for c in cmds:
                o, err, rc = sh(["bash", "-c", c], timeout=900)
                if rc not in (0,) and err.strip():
                    self.cleaning["errors"].append("%s: %s" % (title, err.strip().splitlines()[-1][:200]))
            self.cleaning["done"] += 1
            self.cleaning["freed"] = max(0, shutil.disk_usage(HOME).free - before)
        HUB.send("clean", self.cleaning)
        res = dict(self.cleaning, finished=True)
        self.cleaning = None
        NOTIFIER.push("clean", "Cleanup finished: freed %s" % fmt(res["freed"]),
                      ("%d step(s) had errors." % len(res["errors"])) if res["errors"] else "", {"label": "Open Storage", "open": "storage"},
                      cooldown=0, setting="notify_done", num=res["freed"])
        self.remeasure(only=set(rec_ids))
        HUB.send("clean", res, remember=False)

    def loop(self):
        if not self.data or (self.rescan_min and time.time() - self.last_scan > self.rescan_min * 60):
            self.start_scan()
        tick = 0
        while True:
            time.sleep(30)
            tick += 1
            if self.rescan_min and time.time() - self.last_scan > self.rescan_min * 60:
                self.start_scan()
            elif tick % 4 == 0:  # every ~2 minutes
                try:
                    self.remeasure()
                except Exception as e:
                    print("remeasure:", e, file=sys.stderr)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def proc_list(sampler):
    args = {}
    for line in out(["ps", "-axo", "pid=,etime=,command="], timeout=5).splitlines():
        p = line.strip().split(None, 2)
        if len(p) == 3 and p[0].isdigit():
            args[int(p[0])] = (p[1], p[2][:400])
    with sampler.lock:
        procs = list(sampler.procs.values())
    res = []
    for p in procs:
        a = app_of(p["path"])
        el, cmd = args.get(p["pid"], ("", p["path"]))
        mine = p["user"] in (ME_NAME, str(ME))
        res.append({"pid": p["pid"], "ppid": p["ppid"], "user": p["user"], "cpu": p["cpu"], "mem": p["mem"],
                    "th": p.get("th", 0), "power": p.get("power", 0.0), "name": os.path.basename(p["path"]) or p["path"],
                    "app": os.path.basename(a)[:-4] if a else None, "cmd": cmd, "elapsed": el, "mine": mine,
                    "path": p["path"]})
    res.sort(key=lambda r: -r["cpu"])
    return {"procs": res, "t": time.time()}


def demo_history(rng):
    import math, random
    span = histmod.RANGES.get(rng, 3600)
    n = 360
    now = int(time.time())
    t = [now - span + i * span // n for i in range(n)]
    rnd = random.Random(7)
    def series(base, amp, period, noise, lo=0, hi=None):
        out = []
        for i in range(n):
            v = base + amp * math.sin(2 * math.pi * i / period) + rnd.gauss(0, noise)
            v = max(lo, v if hi is None else min(hi, v))
            out.append(round(v, 2))
        return out
    S = {"t": t, "cpu": series(22, 14, 90, 6, 1, 100), "mem_pct": series(78, 8, 200, 2, 0, 100), "temp": series(64, 10, 90, 3, 30, 105),
         "power": series(24, 12, 90, 4, 2), "cpu_w": series(9, 6, 90, 2, 0), "gpu_w": series(5, 4, 70, 2, 0), "gpu": series(25, 18, 70, 6, 0, 100),
         "net_down": [abs(x) * 1e5 for x in series(12, 10, 40, 6)], "net_up": [abs(x) * 1e4 for x in series(8, 6, 50, 4)],
         "io_read": [abs(x) * 1e6 for x in series(10, 8, 60, 5)], "io_write": [abs(x) * 1e6 for x in series(4, 3, 45, 2)],
         "battery": [max(5, min(100, round(100 - (i % 180) * 0.45 if (i // 180) % 2 == 0 else 20 + (i % 180) * 0.45))) for i in range(n)],
         "disk_free": [36.3e9 - i * 2e7 + (5e9 if i > 300 else 0) for i in range(n)], "swap": series(5e9, 1e9, 300, 1e8, 0),
         "ping": series(14, 3, 50, 2, 3), "mem_used": series(28e9, 3e9, 200, 5e8, 0)}
    for k in list(S):
        if k != "t":
            S[k + "_max"] = [round(v * 1.15, 2) for v in S[k]]
    return {"range": rng, "bucket": span // n, "since": now - span, "first": now - 30 * 86400, "series": S,
            "apps": [{"name": a, "cpu_avg": c, "cpu_max": c * 3, "mem_avg": m * GB, "mem_max": m * 1.3 * GB, "minutes": mi}
                     for a, c, m, mi in [("Xcode", 64.0, 3.6, 40), ("Google Chrome", 21.5, 6.1, 60), ("Cursor", 12.2, 1.4, 60),
                                         ("WindowServer", 9.8, 0.4, 60), ("node", 7.1, 0.8, 55), ("mds_stores", 5.0, 0.3, 20)]],
            "events": [{"ts": now - 2400, "kind": "temp", "text": "Running hot: CPU at 97 °C"},
                       {"ts": now - 9000, "kind": "trash", "text": "~/Downloads/Xcode_16.xip"}]}


def demo_info(names, res):
    """--demo: plausible inventory data for previewing the UI off-Mac."""
    now = time.time()
    D = {
        "security": {"score": 78, "sharing": [{"port": 5900, "name": "Screen Sharing"}, {"port": 7000, "name": "AirPlay Receiver"}],
                     "listening": [22, 631, 5000, 5900, 7000], "items": [
            {"id": "filevault", "title": "FileVault disk encryption", "ok": True, "status": "On", "detail": "Encrypts your disk so a lost or stolen Mac can't be read.", "fix": "", "level": "ok"},
            {"id": "sip", "title": "System Integrity Protection", "ok": True, "status": "Enabled", "detail": "Stops anything, even admin tools, modifying macOS system files.", "fix": "", "level": "ok"},
            {"id": "gatekeeper", "title": "Gatekeeper", "ok": True, "status": "On", "detail": "Only lets apps from the App Store and identified developers open.", "fix": "", "level": "ok"},
            {"id": "firewall", "title": "Firewall", "ok": False, "status": "Off", "detail": "Blocks unwanted incoming connections.", "fix": "System Settings › Network › Firewall", "level": "warn"},
            {"id": "autoupdate", "title": "Automatic security updates", "ok": True, "status": "On", "detail": "Installs security responses automatically.", "fix": "", "level": "ok"},
            {"id": "screenlock", "title": "Password after sleep / screen saver", "ok": True, "status": "Immediately", "detail": "How quickly your Mac asks for a password once it locks.", "fix": "", "level": "ok"},
            {"id": "xprotect", "title": "XProtect malware definitions", "ok": True, "status": "Version 5310", "detail": "Apple's built-in malware scanner.", "fix": "", "level": "ok"},
            {"id": "sharing", "title": "Sharing services", "ok": False, "status": "Screen Sharing, AirPlay Receiver", "detail": "Services other devices can connect to.", "fix": "System Settings › General › Sharing", "level": "info"},
            {"id": "findmy", "title": "Find My Mac", "ok": True, "status": "On", "detail": "Locate, lock or erase this Mac if it's lost.", "fix": "", "level": "ok"}]},
        "apps": [{"name": n, "path": "/Applications/%s.app" % n, "id": "x." + n, "version": v, "size": s * GB, "last_used": now - d * 86400,
                  "appstore": a, "intel_only": i, "archs": ["x86_64"] if i else ["arm64"], "apple": False, "writable": True}
                 for n, v, s, d, a, i in [("Xcode", "26.1", 11.8, 2, True, False), ("Android Studio", "2025.2", 3.1, 41, False, False),
                                          ("Final Cut Pro", "11.1", 4.6, 260, True, False), ("Logic Pro", "11.2", 2.9, 400, True, False),
                                          ("Google Chrome", "141.0", 1.4, 0, False, False), ("Slack", "4.46", 0.6, 0, False, False),
                                          ("Zoom", "6.4", 0.3, 95, False, False), ("Steam", "2.0", 0.4, 210, False, True),
                                          ("Docker", "4.47", 2.2, 1, False, False), ("Cursor", "1.8", 0.7, 0, False, False)]],
        "brew": {"formulae": 182, "casks": 23, "cache": "~/Library/Caches/Homebrew", "cache_size": 2.4 * GB, "prefix": "/opt/homebrew", "version": ["Homebrew 4.6.3"],
                 "outdated": [{"name": "node", "kind": "formula", "have": "24.8.0", "want": "24.10.0"}, {"name": "python@3.13", "kind": "formula", "have": "3.13.6", "want": "3.13.8"},
                              {"name": "visual-studio-code", "kind": "cask", "have": "1.104", "want": "1.105"}],
                 "services": [{"name": "postgresql@16", "status": "started"}, {"name": "redis", "status": "none"}]},
        "updates": {"checked": now - 3600, "updates": [{"label": "macOS Tahoe 26.1.1-25B90", "title": "macOS Tahoe 26.1.1", "version": "26.1.1", "size": "2.1GiB", "restart": True}]},
        "background": {"login_items": ["Raycast", "Rectangle", "Dropbox"], "agents": [
            {"label": "com.google.keystone.agent", "scope": "user", "file": "~/Library/LaunchAgents/com.google.keystone.agent.plist", "program": "~/Library/Google/GoogleSoftwareUpdate/…/GoogleSoftwareUpdateAgent", "missing": False, "loaded": True, "running": False, "run_at_load": True, "keep_alive": False, "disabled": False, "removable": True},
            {"label": "com.oldvpn.helper", "scope": "user", "file": "~/Library/LaunchAgents/com.oldvpn.helper.plist", "program": "/Applications/OldVPN.app/Contents/MacOS/helper", "missing": True, "loaded": False, "running": False, "run_at_load": True, "keep_alive": True, "disabled": False, "removable": True},
            {"label": "com.docker.vmnetd", "scope": "system", "file": "/Library/LaunchDaemons/com.docker.vmnetd.plist", "program": "/Library/PrivilegedHelperTools/com.docker.vmnetd", "missing": False, "loaded": False, "running": True, "run_at_load": True, "keep_alive": True, "disabled": False, "removable": False}]},
        "tools": [{"name": n, "version": v, "path": "/opt/homebrew/bin/" + n} for n, v in [("git", "2.51.0"), ("node", "24.8.0"), ("pnpm", "10.17.1"), ("bun", "1.2.22"), ("python3", "3.13.6"),
                  ("uv", "0.8.22"), ("go", "1.25.1"), ("docker", "28.4.0"), ("kubectl", "1.34.1"), ("terraform", "1.13.3"), ("aws", "2.31.0"), ("databricks", "0.270.0"),
                  ("gh", "2.80.0"), ("ollama", "0.12.3"), ("claude", "2.1.301"), ("xcodebuild", "26.1")]],
        "repos": [{"path": "~/projects/" + n, "name": n, "branch": b, "dirty": d, "untracked": u, "ahead": a, "behind": bh, "no_upstream": nu,
                   "last_commit": now - age * 3600, "last_msg": m, "remote": "git@github.com:you/%s.git" % n, "stashes": st}
                  for n, b, d, u, a, bh, nu, age, m, st in [("ios-app", "main", 3, 1, 2, 0, False, 2, "Onboarding flow", 0), ("mobile-app", "feat/settings", 12, 4, 0, 0, True, 20, "WIP settings screen", 1),
                                                          ("diskmap", "main", 0, 0, 0, 0, False, 1, "Hardware panels", 0), ("website", "main", 0, 0, 0, 3, False, 300, "Design tokens", 0),
                                                          ("infra", "main", 1, 0, 5, 0, False, 70, "Helm values", 2)]],
        "docker": {"running": True, "usage": [{"Type": "Images", "TotalCount": "14", "Active": "2", "Size": "9.8GB", "Reclaimable": "7.1GB (72%)"},
                                              {"Type": "Containers", "TotalCount": "5", "Active": "0", "Size": "120MB", "Reclaimable": "120MB (100%)"},
                                              {"Type": "Local Volumes", "TotalCount": "6", "Active": "1", "Size": "2.3GB", "Reclaimable": "1.9GB (82%)"},
                                              {"Type": "Build Cache", "TotalCount": "88", "Active": "0", "Size": "4.4GB", "Reclaimable": "4.4GB"}],
                   "containers": [{"name": "redis", "image": "redis:7", "status": "Exited (0) 3 days ago", "state": "exited", "ports": ""},
                                  {"name": "pg", "image": "postgres:16", "status": "Exited (0) 3 days ago", "state": "exited", "ports": ""}]},
        "ollama": [{"name": "deepseek-coder:6.7b", "size": 3.8 * GB, "modified": "2026-09-30T10:00:00", "family": "llama", "params": "7B", "quant": "Q4_0"},
                   {"name": "llama3.1:8b", "size": 4.9 * GB, "modified": "2026-08-14T10:00:00", "family": "llama", "params": "8.0B", "quant": "Q4_K_M"}],
        "logs": {"crash_counts": [["Xcode", 4], ["Simulator", 2], ["node", 1]], "reboots": ["Sep 19 17:05"], "shutdowns": ["Sep 19 17:04"],
                 "crashes": [{"app": "Xcode", "file": "~/Library/Logs/DiagnosticReports/Xcode-2026-10-14-101010.ips", "ts": now - 86400, "kind": "crash"},
                             {"app": "Simulator", "file": "~/Library/Logs/DiagnosticReports/Simulator-2026-10-11.ips", "ts": now - 4 * 86400, "kind": "hang"},
                             {"app": "Kernel", "file": "/Library/Logs/DiagnosticReports/panic-full-2026-09-19.panic", "ts": now - 26 * 86400, "kind": "panic"}],
                 "sleep": [{"ts": "2026-10-15 08:01:12", "kind": "Wake", "reason": "Wake from Deep Idle [CDNVA] : due to UserActivity Assertion"},
                           {"ts": "2026-10-15 00:42:03", "kind": "Sleep", "reason": "Entering Sleep state due to 'Clamshell Sleep'"},
                           {"ts": "2026-10-14 19:15:40", "kind": "DarkWake", "reason": "DarkWake from Deep Idle [CDN] : due to RTC/Maintenance"}]},
        "netx": {"dns": ["192.168.1.1", "1.1.1.1"], "connections": [["Google Ch", 64], ["Slack", 9], ["Cursor", 7], ["Dropbox", 5]], "total_connections": 91,
                 "top_remotes": [["142.250.80.46", 12], ["3.33.152.147", 6]], "vpn": [{"name": "Tailscale", "status": "Connected"}], "public_ip": None,
                 "proxy_on": False, "hostname": "Demo-MacBook-Pro"},
        "hardware": {"SPHardwareDataType": [{"_name": "hardware_overview", "machine_name": "MacBook Pro", "model_number": "Z1FE0", "chip_type": "Apple M4 Max",
                                             "number_processors": "proc 16:12:4", "physical_memory": "36 GB", "serial_number": "•••XY7Q", "os_loader_version": "13822.1.2"}],
                     "SPNVMeDataType": [{"_name": "Apple SSD Controller", "_items": [{"_name": "APPLE SSD AP1024Z", "size": "1 TB", "spnvme_trim_support": "Yes", "smart_status": "Verified"}]}],
                     "SPAudioDataType": [{"_name": "Devices", "_items": [{"_name": "MacBook Pro Speakers", "coreaudio_output_source": "MacBook Pro Speakers"}, {"_name": "AirPods Pro"}]}]},
    }
    for n in names:
        if n in D:
            res[n] = {"data": D[n], "updated": now - 30, "busy": False}
    return res


DIAG_CACHE = {}


def diagnostics(sampler, disk):
    """Checks everything diskmap depends on, with a fix for each problem."""
    snap = sampler.snapshot or {}
    hw = snap.get("hw") or {}
    hs = snap.get("hw_slow") or {}
    checks = []

    def add(group, id_, title, status, detail, fix=None):
        checks.append({"group": group, "id": id_, "title": title, "status": status, "detail": detail, "fix": fix})
    open_pane = lambda pane, label="Open Settings": {"label": label, "kind": "open_settings", "pane": pane}

    # Basics
    add("Basics", "python", "Python", "ok", "Python %s at %s" % (sys.version.split()[0], sys.executable))
    add("Basics", "mode", "Running as", "ok", "Mac app" if APP_MODE else "Browser dashboard (server.py)")
    add("Basics", "top", "Process stats (top)", "ok" if sampler.top_ok else ("fail" if MAC else "unknown"),
        "Per-process CPU, memory, threads and energy" if sampler.top_ok else "Couldn't read top; using ps instead (less accurate).")
    # Sensors
    errs = sampler.mac.errors if sampler.mac else {}
    sens = [("temps", "Temperature sensors", bool((hw.get("temps") or {}).get("sensors")), "%d sensors" % len((hw.get("temps") or {}).get("sensors") or [])),
            ("smc", "Fans & system power (SMC)", bool(hw.get("fans") or hw.get("smc_power")), "%d fans" % len(hw.get("fans") or [])),
            ("ioreport", "CPU / GPU / ANE power", bool(hw.get("power")), "%.1f W CPU now" % (hw.get("power") or {}).get("cpu", 0) if hw.get("power") else ""),
            ("cores", "Per-core load", bool(hw.get("cores")), "%d cores" % len(hw.get("cores") or [])),
            ("thermal", "Thermal pressure", hw.get("thermal") is not None, str(hw.get("thermal") or "")),
            ("battery", "Battery", bool(hw.get("battery")), "%s%%" % (hw.get("battery") or {}).get("percent") if hw.get("battery") else "No battery (desktop Mac)")]
    for key, title, ok, detail in sens:
        if not MAC:
            add("Sensors", key, title, "unknown", "macOS only")
        elif ok:
            add("Sensors", key, title, "ok", detail)
        elif key == "battery":
            add("Sensors", key, title, "info", detail)
        else:
            add("Sensors", key, title, "fail", errs.get(key) or "No readings yet")
    wf = hs.get("wifi")
    if wf and not wf.get("off"):
        named = wf.get("ssid") and "redacted" not in str(wf.get("ssid")).lower()
        add("Sensors", "wifi", "Wi-Fi details", "ok" if named else "warn",
            ("Connected to %s" % wf["ssid"]) if named else "Network name hidden: macOS needs Location access for it.",
            None if named else open_pane("location", "Location settings"))
    # Permissions
    fda_paths = [os.path.join(HOME, "Library", x) for x in ("Mail", "Safari", "Messages")]
    fda = None
    for pth in fda_paths:
        if os.path.exists(pth):
            try:
                os.listdir(pth)
                fda = True
            except PermissionError:
                fda = False
            break
    who = "diskmap" if APP_MODE else "your terminal app"
    add("Permissions", "fda", "Full Disk Access", "ok" if fda else ("unknown" if fda is None else "warn"),
        "Complete disk map, all logs and Trash" if fda else "Some folders can't be scanned. Add %s under Full Disk Access." % who,
        None if fda else open_pane("fda"))
    for b, info in (sampler.tabs or {}).items():
        st = info.get("status")
        add("Permissions", "auto-" + b, "Control %s (tabs)" % b, "ok" if st == "ok" else "warn",
            "%d tabs visible" % len(info.get("tabs", [])) if st == "ok" else "Allow %s to control %s." % (who, b),
            None if st == "ok" else open_pane("automation"))
    add("Permissions", "finder", "Control Finder (move to Trash)", "ok" if DIAG_CACHE.get("finder") else "unknown",
        "Works" if DIAG_CACHE.get("finder") else "Not tested yet. macOS asks the first time.",
        None if DIAG_CACHE.get("finder") else {"label": "Test now", "kind": "test_finder"})
    bg = (INV.data.get("background") if INV else None) or {}
    ev_ok = DIAG_CACHE.get("events") or bg.get("login_items") is not None
    add("Permissions", "events", "Control System Events (login items)", "ok" if ev_ok else "unknown",
        "Works" if ev_ok else "Needed to list login items.", None if ev_ok else {"label": "Test now", "kind": "test_events"})
    add("Permissions", "notify", "Notifications", "info",
        "Shown by the diskmap app" if APP_MODE else "Shown via Script Editor; allow them in Notification settings.",
        {"label": "Send test", "kind": "notify_test"})
    # Disk & data
    d = disk.data or {}
    add("Data", "scan", "Disk scan", "ok" if d else ("info" if disk.scanning else "warn"),
        ("%s files, %s, %s" % (format(d.get("files", 0), ","), dm.human(d.get("scanned", 0)), "scanned " + time.strftime("%b %d %H:%M", time.localtime(disk.last_scan))))
        if d else ("Scanning now…" if disk.scanning else "No scan yet"), {"label": "Rescan", "kind": "rescan"})
    if d.get("unreadable_count"):
        add("Data", "unread", "Unreadable folders", "warn", "%d folders skipped" % d["unreadable_count"], open_pane("fda"))
    if HISTORY:
        add("Data", "history", "History database", "ok", "%s · keeps %s days · %s" % (dm.human(HISTORY.size()), SETTINGS["history_days"], dm.tilde(HISTORY.path)))
    # Tools
    env_path = os.pathsep.join([os.environ.get("PATH", "")] + inv.PATHS)
    for tool, why in (("brew", "Homebrew panel and upgrades"), ("git", "Git repos panel"), ("docker", "Docker panel"),
                      ("ollama", "Local AI models"), ("sqlite3", "Compacting editor databases"), ("lsof", "Ports and connections")):
        w = shutil.which(tool, path=env_path)
        add("Tools", tool, tool, "ok" if w else "info", (dm.tilde(w) + " · " + why) if w else "Not installed · " + why)
    # Sections
    for name, (fn, ttl) in inv.SECTIONS.items():
        e = (INV.data.get(name) if INV else None)
        when = INV.when.get(name) if INV else None
        err = isinstance(e, dict) and e.get("error")
        add("Collectors", "sec-" + name, name, "fail" if err else ("ok" if when else "unknown"),
            err or ("updated " + time.strftime("%H:%M", time.localtime(when)) if when else "not collected yet"),
            {"label": "Refresh", "kind": "refresh", "sections": [name]})
    return {"checks": checks, "errors": list(ERRLOG)[-30:], "t": time.time(),
            "counts": collections.Counter(c["status"] for c in checks)}


def home_payload(sampler, disk):
    d = disk.data or {}
    demo = sampler.demo
    growth = demo_growth() if demo else GROWTH.result
    return {"growth": [{k: v for k, v in g.items() if k != "series"} | {"series": g.get("series", [])[-60:]} for g in growth],
            "watch": list(WATCHER.events)[:5] if not demo else demo_watch(),
            "focus": SETTINGS.get("focus_active") or {}, "profiles": SETTINGS.get("profiles", []),
            "safe": sum(r["s"] for r in d.get("recs", []) if r["tier"] == "safe"),
            "review": sum(r["s"] for r in d.get("recs", []) if r["tier"] == "review"),
            "dups": sum(g["waste"] for g in d.get("dups", [])), "scanned": d.get("scanned"),
            "last_scan": disk.last_scan}


def demo_growth():
    now = time.time()
    mk = lambda a, b, n=90: [[now - (n - i) * 3600, a + (b - a) * (i / n) ** 1.3] for i in range(n)]
    return [{"name": "Safari", "key": "/Applications/Safari.app", "app": "/Applications/Safari.app", "start": 2.8 * GB, "now": 9.4 * GB,
             "peak": 9.6 * GB, "age": 3.8 * 86400, "slope": 1.7 * GB, "growth": 6.6 * GB, "flag": True, "protected": False, "series": mk(2.8 * GB, 9.4 * GB)},
            {"name": "Cursor", "key": "/Applications/Cursor.app", "app": "/Applications/Cursor.app", "start": 1.1 * GB, "now": 3.4 * GB,
             "peak": 3.5 * GB, "age": 2.1 * 86400, "slope": 1.1 * GB, "growth": 2.3 * GB, "flag": True, "protected": False, "series": mk(1.1 * GB, 3.4 * GB, 50)},
            {"name": "Slack", "key": "/Applications/Slack.app", "app": "/Applications/Slack.app", "start": 0.9 * GB, "now": 1.3 * GB,
             "peak": 1.4 * GB, "age": 5 * 86400, "slope": 0.08 * GB, "growth": 0.4 * GB, "flag": False, "protected": False, "series": mk(0.9 * GB, 1.3 * GB, 120)}]


def demo_watch():
    return [{"t": time.time() - 5400, "drop": 6.4 * GB, "secs": 420, "swap": 0,
             "found": [{"p": "~/Downloads/Xcode_26.1.xip", "s": 6.1 * GB, "how": "new or changed file"}]}]


def demo_battery():
    now = time.time()
    daily = [{"day": time.strftime("%Y-%m-%d", time.localtime(now - (40 - i) * 86400)), "health": round(97.8 - i * 0.04, 1),
              "cycles": 120 + i // 2, "max_mah": 6100 - i * 3, "design_mah": 6249, "temp": 31 + (i % 5)} for i in range(41)]
    sessions = [{"start": now - (k * 86400 + 30000), "end": now - (k * 86400 + 30000) + h * 3600, "hours": h, "from": 100, "to": round(100 - r * h),
                 "rate": r, "full_hours": round(100 / r, 1), "avg_w": w} for k, (h, r, w) in enumerate([(2.5, 11.2, 9.8), (1.2, 18.5, 16.1), (3.1, 9.4, 8.2), (0.8, 22.0, 19.5), (2.2, 12.6, 11.0)])]
    return {"daily": daily, "sessions": sessions, "cycles_per_week": 3.4, "typical_rate": 12.6, "typical_full_hours": 7.9,
            "hogs": [{"name": "Safari", "energy": 4200, "cpu": 14}, {"name": "Xcode", "energy": 3100, "cpu": 40}, {"name": "Cursor", "energy": 1900, "cpu": 9},
                     {"name": "WindowServer", "energy": 1200, "cpu": 8}]}


def demo_digest():
    return {"days": 7, "since": time.time() - 7 * 86400, "samples": 25000, "hours_on": 69.4, "cpu_avg": 17.2, "cpu_max": 100, "mem_avg": 81.8,
            "swap_max": 14.8 * GB, "temp_avg": 48.6, "temp_max": 64, "power_avg": 26.9, "net_down": 61 * GB, "net_up": 6.2 * GB,
            "disk_written": 410 * GB, "free_change": -4.6 * GB, "freed": 23.4 * GB, "hot_minutes": 0,
            "top_cpu": [{"name": "Safari", "cpu": 14.0, "mem": 6.1 * GB, "mem_max": 9.6 * GB, "energy": 22}, {"name": "Xcode", "cpu": 9.1, "mem": 2.7 * GB, "mem_max": 4.1 * GB, "energy": 31},
                        {"name": "Cursor", "cpu": 6.4, "mem": 2.2 * GB, "mem_max": 3.5 * GB, "energy": 12}],
            "top_mem": [{"name": "Safari", "mem": 6.1 * GB}, {"name": "Docker", "mem": 4.2 * GB}, {"name": "Xcode", "mem": 2.7 * GB}],
            "events": {"clean": {"count": 2, "num": 23.4 * GB}, "crash:Xcode": {"count": 3, "num": 0}, "restart": {"count": 1, "num": 0}}}


def demo_apphistory(name, rng):
    import math, random
    span = histmod.RANGES.get(rng, 86400)
    n = 240
    now = int(time.time())
    rnd = random.Random(hash(name) & 0xffff)
    t = [now - span + i * span // n for i in range(n)]
    base = 10 + rnd.random() * 40
    return {"name": name, "range": rng, "t": t,
            "cpu": [max(0, base + 25 * math.sin(i / 12) + rnd.gauss(0, 6)) if i % 70 < 60 else None for i in range(n)],
            "mem": [(1.5 + 0.6 * math.sin(i / 30) + rnd.random() * 0.1) * GB if i % 70 < 60 else None for i in range(n)],
            "energy": [max(0, base * 0.8 + 15 * math.sin(i / 12)) if i % 70 < 60 else None for i in range(n)],
            "stats": {"cpu_avg": base, "cpu_max": base * 3, "mem_avg": 1.5 * GB, "mem_max": 2.3 * GB, "energy_avg": base * 0.8, "minutes": 900}}


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def act(body, sampler, disk):
    kind = body.get("kind")
    with sampler.lock:
        groups = dict(sampler.groups)
    if kind == "quit_app":
        app = body.get("app") or ""
        g = groups.get(app)
        if not g or g["protected"]:
            raise ValueError("That app isn't running or can't be quit from here.")
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                bid = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, ValueError):
            bid = None
        target = ("application id %s" % as_quote(bid)) if bid else ("application %s" % as_quote(g["name"]))
        o, err, rc = osascript("tell %s to quit" % target, timeout=20)
        if rc != 0:
            raise RuntimeError(err.strip() or "Couldn't quit %s" % g["name"])
        return "Asked %s to quit." % g["name"]
    if kind == "stop_group":
        g = groups.get(body.get("key"))
        if not g or not g["mine"] or g["system"] or g["name"] in PROTECTED:
            raise ValueError("Can't stop that process from here.")
        n = 0
        for pid in g["pids"]:
            try:
                os.kill(pid, signal.SIGTERM)
                n += 1
            except OSError:
                pass
        return "Sent stop signal to %d %s process%s." % (n, g["name"], "" if n == 1 else "es")
    if kind == "stop_pid":
        pid = int(body.get("pid", 0))
        if pid not in {l["pid"] for l in sampler.listeners} or pid == MYPID:
            raise ValueError("That server isn't running any more.")
        os.kill(pid, signal.SIGTERM)
        return "Stopped process %d." % pid
    if kind == "close_tabs":
        b = body.get("browser")
        current = {(t["win"], t["id"]) for t in (sampler.tabs.get(b) or {}).get("tabs", [])}
        tabs = [t for t in body.get("tabs", []) if (str(t.get("win")), str(t.get("id"))) in current]
        if not tabs:
            raise ValueError("Those tabs are already closed.")
        if b == "Safari":  # indexes shift while closing, so go from the end
            tabs.sort(key=lambda t: (t["win"], -int(t["id"])))
        n = close_tabs(b, tabs)
        return "Closed %d tab%s in %s." % (n, "" if n == 1 else "s", b)
    if kind == "unload_model":
        name = body.get("model")
        if name not in {m["name"] for m in sampler.models}:
            raise ValueError("That model isn't loaded.")
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                     data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return "Unloaded %s." % name
    if kind == "kill_pid":
        pid = int(body.get("pid", 0))
        with sampler.lock:
            pr = sampler.procs.get(pid)
        if not pr or pid in (MYPID, 1) or pr["user"] not in (ME_NAME, str(ME)):
            raise ValueError("You can only stop your own processes.")
        name = os.path.basename(pr["path"])
        if name in PROTECTED or os.path.basename(app_of(pr["path"]) or "")[:-4] in PROTECTED:
            raise ValueError("%s is protected." % name)
        sig = signal.SIGKILL if body.get("force") else signal.SIGTERM
        os.kill(pid, sig)
        return "%s %s (%d)." % ("Force-killed" if body.get("force") else "Stopped", name, pid)
    if kind in ("reveal", "trash"):
        path = os.path.realpath(os.path.expanduser(body.get("path", "")))
        allowed = path.startswith(HOME + os.sep) or path.startswith("/Applications/")
        if not allowed or not os.path.lexists(path) or path in (HOME,):
            raise ValueError("That file isn't available.")
        if kind == "reveal":
            subprocess.Popen(["open", "-R", path])
            return "Shown in Finder."
        known = set()
        d = disk.data or {}
        known |= {os.path.realpath(os.path.expanduser(x["p"])) for x in d.get("large", [])}
        known |= {os.path.realpath(os.path.expanduser(f["p"])) for g in d.get("dups", []) for f in g["files"]}
        known |= {a["path"] for a in (INV.data.get("apps") or []) if not a.get("apple")} if INV else set()
        if path not in known:
            raise ValueError("Only files and apps listed on this page can be moved to the Trash.")
        trashed_size = dm.measure(path)
        o, err, rc = osascript('tell application "Finder" to delete (POSIX file %s as alias)' % as_quote(path), timeout=60)
        if rc != 0:
            raise RuntimeError(err.strip() or "Finder couldn't move it to the Trash.")
        if HISTORY:
            HISTORY.event("trash", dm.tilde(path), trashed_size)
        if INV and path.endswith(".app"):
            INV.request(["apps"], force=True)
        return "Moved %s to the Trash (you can put it back from there)." % os.path.basename(path)
    if kind == "remove_agent":
        f = os.path.realpath(os.path.expanduser(body.get("file", "")))
        agents_dir = os.path.join(HOME, "Library", "LaunchAgents")
        if os.path.dirname(f) != agents_dir or not f.endswith(".plist") or not os.path.isfile(f):
            raise ValueError("Only your own launch agents can be removed here.")
        sh(["launchctl", "bootout", "gui/%d" % ME, f], timeout=10)
        o, err, rc = osascript('tell application "Finder" to delete (POSIX file %s as alias)' % as_quote(f), timeout=30)
        if rc != 0:
            raise RuntimeError(err.strip() or "Couldn't remove it.")
        if INV:
            INV.request(["background"], force=True)
        return "Stopped and moved %s to the Trash." % os.path.basename(f)
    if kind == "open_settings":
        panes = {"security": "x-apple.systempreferences:com.apple.preference.security",
                 "sharing": "x-apple.systempreferences:com.apple.Sharing-Settings.extension",
                 "updates": "x-apple.systempreferences:com.apple.Software-Update-Settings.extension",
                 "lock": "x-apple.systempreferences:com.apple.Lock-Screen-Settings.extension",
                 "network": "x-apple.systempreferences:com.apple.Network-Settings.extension",
                 "battery": "x-apple.systempreferences:com.apple.Battery-Settings.extension",
                 "login": "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
                 "storage": "x-apple.systempreferences:com.apple.settings.Storage",
                 "privacy": "x-apple.systempreferences:com.apple.preference.security?Privacy",
                 "icloud": "x-apple.systempreferences:com.apple.systempreferences.AppleIDSettings",
                 "fda": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
                 "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
                 "location": "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices",
                 "notifications": "x-apple.systempreferences:com.apple.Notifications-Settings.extension"}
        url = panes.get(body.get("pane"))
        if not url:
            raise ValueError("Unknown settings pane.")
        subprocess.Popen(["open", url])
        return "Opened System Settings."
    if kind == "open_app":
        name = body.get("app", "")
        if name not in ("Activity Monitor", "Console", "Disk Utility", "System Information", "Terminal", "Software Update"):
            raise ValueError("Unknown app.")
        subprocess.Popen(["open", "-a", name])
        return "Opened %s." % name
    if kind == "restart_app":
        app = body.get("app") or ""
        g = groups.get(app)
        if not g or g["protected"]:
            raise ValueError("That app isn't running or can't be restarted from here.")
        pids = list(g["pids"])
        act({"kind": "quit_app", "app": app}, sampler, disk)

        def relaunch():
            for _ in range(60):
                if not any(_alive(p) for p in pids):
                    break
                time.sleep(0.5)
            subprocess.Popen(["open", "-a", app])
        threading.Thread(target=relaunch, daemon=True).start()
        if HISTORY:
            HISTORY.event("restart", "Restarted %s (%s)" % (g["name"], fmt(g["mem"])), g["mem"])
        return "Restarting %s…" % g["name"]
    if kind == "focus_start":
        return Focus.start(body.get("name", ""), sampler)
    if kind == "focus_end":
        return Focus.end(sampler)
    if kind == "save_profiles":
        profs = []
        for pr in (body.get("profiles") or [])[:12]:
            name = str(pr.get("name", "")).strip()[:40]
            if not name:
                continue
            profs.append({"name": name, "icon": str(pr.get("icon", ""))[:4],
                          "quit": [str(x)[:60] for x in (pr.get("quit") or [])][:60],
                          "unload_models": bool(pr.get("unload_models")), "close_heavy_tabs": bool(pr.get("close_heavy_tabs")),
                          "shortcut_on": str(pr.get("shortcut_on", ""))[:80], "shortcut_off": str(pr.get("shortcut_off", ""))[:80]})
        SETTINGS["profiles"] = profs
        save_settings()
        return "Saved %d profile%s." % (len(profs), "" if len(profs) == 1 else "s")
    if kind == "task":
        return TASKS.start(body.get("task"))
    if kind == "refresh":
        if INV:
            INV.request([x for x in body.get("sections", []) if x in inv.SECTIONS], force=True)
        return "Refreshing…"
    if kind == "settings":
        for k, v in (body.get("values") or {}).items():
            if k in DEFAULT_SETTINGS and k not in ("profiles", "focus_active") and type(v) in (type(DEFAULT_SETTINGS[k]), int, float, bool):
                if isinstance(v, str) and (len(v) > 24 or not k.startswith("ui_")):
                    continue
                SETTINGS[k] = v
        save_settings()
        if all(k.startswith("ui_") for k in (body.get("values") or {})):
            return "Saved."
        if INV:
            INV.settings = SETTINGS
            INV.request(["netx"], force=True)
        return "Settings saved."
    if kind == "notify_test":
        NOTIFIER.push("test", "Notifications are working", "This is what diskmap alerts look like.",
                      {"label": "Open diskmap", "open": "overview"}, cooldown=0)
        return "Sent a test notification."
    if kind == "test_finder":
        o, err, rc = osascript('tell application "Finder" to get name of startup disk', timeout=30)
        if rc != 0:
            raise RuntimeError("Finder access was refused. Allow it under Privacy & Security › Automation.")
        DIAG_CACHE["finder"] = True
        return "Finder access works (startup disk: %s)." % o.strip()
    if kind == "test_events":
        o, err, rc = osascript('tell application "System Events" to get the name of every login item', timeout=30)
        if rc != 0:
            raise RuntimeError("System Events access was refused. Allow it under Privacy & Security › Automation.")
        DIAG_CACHE["events"] = True
        if INV:
            INV.request(["background"], force=True)
        return "System Events access works."
    if kind == "rescan":
        return "Rescan started." if disk.start_scan() else "A scan is already running."
    if kind == "clean":
        disk.clean(body.get("items", []), body.get("scan_id"))
        return "Cleanup started."
    raise ValueError("Unknown action.")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "diskmap"
    token = ""
    sampler = None
    disk = None
    port = 7777
    lan = False
    icons = {}

    def log_message(self, *a):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        if host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            return True
        return self.lan and not self._local()

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/widget":
            if not self._local():
                return self._send(403, "local only", "text/plain")
            with open(os.path.join(dm.HERE, "widget.html"), encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", self.token)
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/":
            live = {"token": self.token if self._local() else "", "readonly": not self._local(),
                    "prefs": {k: v for k, v in SETTINGS.items() if k.startswith("ui_")}}
            return self._send(200, dm.render_html(None, live=live), "text/html; charset=utf-8")
        if u.path == "/api/icon":
            return self._icon(q.get("p", [""])[0])
        if q.get("t", [""])[0] != self.token and self._local():
            return self._send(403, {"error": "bad token"})
        if u.path == "/api/history":
            rng = q.get("range", ["1h"])[0]
            if self.sampler.demo:
                return self._send(200, demo_history(rng))
            if not HISTORY:
                return self._send(200, {"series": {"t": []}, "apps": [], "events": []})
            return self._send(200, HISTORY.query(rng))
        if u.path == "/api/info":
            names = [n for n in q.get("s", [""])[0].split(",") if n]
            res = INV.get(names) if INV else {}
            if self.sampler.demo:
                res = demo_info(names, res)
            return self._send(200, res)
        if u.path == "/api/procs":
            return self._send(200, proc_list(self.sampler))
        if u.path == "/api/home":
            return self._send(200, home_payload(self.sampler, self.disk))
        if u.path == "/api/battery":
            if self.sampler.demo:
                return self._send(200, demo_battery())
            return self._send(200, HISTORY.battery_report() if HISTORY else {})
        if u.path == "/api/digest":
            days = max(1, min(30, int(q.get("days", ["7"])[0] or 7)))
            d = demo_digest() if self.sampler.demo else (HISTORY.digest(days) if HISTORY else {})
            d["text"] = digest_text(d) if d else ""
            return self._send(200, d)
        if u.path == "/api/apphistory":
            name = q.get("name", [""])[0]
            rng = q.get("range", ["24h"])[0]
            if self.sampler.demo:
                return self._send(200, demo_apphistory(name, rng))
            return self._send(200, HISTORY.app_query(name, rng) if HISTORY else {"t": []})
        if u.path == "/api/diag":
            return self._send(200, diagnostics(self.sampler, self.disk))
        if u.path == "/api/summary":
            sn = self.sampler.snapshot or {}
            hw = sn.get("hw") or {}
            mem = sn.get("mem") or {}
            sugg = sn.get("suggestions") or []
            return self._send(200, {
                "cpu": (sn.get("cpu") or {}).get("total"), "mem_used": mem.get("used"), "mem_total": mem.get("total"),
                "pressure": mem.get("pressure"), "swap": mem.get("swap"), "temp": (hw.get("temps") or {}).get("cpu_max"),
                "power": hw.get("sys_power"), "battery": (hw.get("battery") or {}).get("percent"),
                "charging": (hw.get("battery") or {}).get("charging"), "disk_free": (sn.get("disk") or {}).get("free"),
                "gpu": (sn.get("gpu") or {}).get("util"), "net_down": (hw.get("net") or {}).get("down"),
                "net_up": (hw.get("net") or {}).get("up"), "alerts": sum(1 for x in sugg if x["sev"] in ("high", "med")),
                "top": [x["title"] for x in sugg if x["sev"] in ("high", "med")][:4],
                "scanning": bool(self.disk.scanning),
                "cpu_hist": list(self.sampler.cpu_hist)[-40:],
                "notes": NOTIFIER.since(int(q.get("since", ["0"])[0] or 0)),
                "seq": NOTIFIER.seq})
        if u.path == "/api/settings":
            return self._send(200, dict(SETTINGS, history_size=HISTORY.size() if HISTORY else 0,
                                        task=TASKS.cur, data_dir=dm.tilde(DATA_DIR)))
        if u.path == "/api/disk":
            d = self.disk.data
            return self._send(200, d if d else {"pending": True, "status": self.disk.status()})
        if u.path == "/api/events":
            return self._events()
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not (self._local() and self._host_ok()):
            return self._send(403, {"error": "Actions only work on this Mac."})
        if self.headers.get("X-Token") != self.token:
            return self._send(403, {"error": "bad token"})
        u = urlparse(self.path)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if u.path != "/api/action":
            return self._send(404, {"error": "not found"})
        try:
            msg = act(body, self.sampler, self.disk)
            self.sampler.poke(slow=body.get("kind") in ("close_tabs", "unload_model", "stop_pid", "quit_app"))
            return self._send(200, {"ok": True, "msg": msg})
        except Exception as e:
            return self._send(400, {"ok": False, "error": str(e)})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.add()
        try:
            with HUB.lock:
                initial = list(HUB.last.values())
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.write(("event: scan\ndata: %s\n\n" % json.dumps(self.disk.status())).encode())
            for m in initial:
                self.wfile.write(m)
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.remove(q)

    def _icon(self, p):
        p = os.path.realpath(p)
        if not (MAC and p.endswith(".app") and os.path.isdir(p)):
            return self._send(404, b"", "image/png")
        if p not in self.icons:
            png = None
            try:
                with open(os.path.join(p, "Contents", "Info.plist"), "rb") as f:
                    info = plistlib.load(f)
                name = info.get("CFBundleIconFile") or info.get("CFBundleIconName") or ""
                if name and not name.endswith(".icns"):
                    name += ".icns"
                icns = os.path.join(p, "Contents", "Resources", name)
                if name and os.path.isfile(icns):
                    with tempfile.TemporaryDirectory() as td:
                        dst = os.path.join(td, "i.png")
                        sh(["sips", "-s", "format", "png", "-Z", "64", icns, "--out", dst], timeout=5)
                        if os.path.isfile(dst):
                            with open(dst, "rb") as f:
                                png = f.read()
            except (OSError, ValueError):
                pass
            self.icons[p] = png
        png = self.icons[p]
        if not png:
            return self._send(404, b"", "image/png")
        return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})


def main():
    ap = argparse.ArgumentParser(description="Live local dashboard: disk map, CPU/RAM, suggested actions.")
    ap.add_argument("--port", type=int, default=7777, help="0 = pick a free port")
    ap.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--lan", action="store_true", help="also serve to other devices on your network (read-only there)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between live samples (default 2)")
    ap.add_argument("--rescan-min", type=int, default=60, help="full disk rescan every N minutes (0 = only on demand)")
    ap.add_argument("--idle-days", type=int, default=30)
    ap.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    global HISTORY, INV
    HISTORY = histmod.History(os.path.join(DATA_DIR, "history.db"), SETTINGS.get("history_days", 30))
    def on_info(n, v, busy):
        HUB.send("info", {"section": n, "busy": busy}, remember=False)
        if n == "logs" and not busy and isinstance(v, dict):
            NOTIFIER.check_logs(v)
    INV = inv.Inventory(on_update=on_info, settings=SETTINGS)
    if not args.demo:
        INV.start()
    sampler = Sampler(args.interval, demo=args.demo)
    disk = Disk(args.rescan_min, args.idle_days)
    Handler.token = os.environ.get("DISKMAP_TOKEN") or secrets.token_urlsafe(24)
    Handler.sampler, Handler.disk, Handler.port, Handler.lan = sampler, disk, args.port, args.lan

    threading.Thread(target=sampler.run, daemon=True).start()
    threading.Thread(target=disk.loop, daemon=True).start()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        httpd = ThreadingHTTPServer((host, args.port), Handler)
    except OSError as e:
        sys.exit("Port %d is busy (%s). Try --port 7778." % (args.port, e))
    httpd.daemon_threads = True
    args.port = Handler.port = httpd.server_address[1]
    print("DISKMAP_PORT=%d" % args.port, flush=True)
    if args.parent_pid:
        def watchdog(ppid):
            while True:
                time.sleep(3)
                try:
                    os.kill(ppid, 0)
                except OSError:
                    os._exit(0)  # the app went away: stop too
        threading.Thread(target=watchdog, args=(args.parent_pid,), daemon=True).start()
    url = "http://127.0.0.1:%d/" % args.port
    print("diskmap live · %s" % url, file=sys.stderr)
    if args.lan:
        print("  also on your network at http://%s.local:%d/ (read-only)" % (dm.socket.gethostname().split(".")[0], args.port),
              file=sys.stderr)
    if not disk.data:
        print("  first disk scan running in the background (a couple of minutes)…", file=sys.stderr)
    print("  Ctrl-C to stop", file=sys.stderr)
    if not args.no_open:
        if MAC:
            subprocess.Popen(["open", url])
        else:
            import webbrowser
            webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


if __name__ == "__main__":
    main()
