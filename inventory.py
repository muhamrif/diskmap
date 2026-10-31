"""
inventory — the slower, "everything about this Mac" collectors:
security posture, installed software, background items, developer tools and
git repos, a full hardware inventory, crash logs and sleep/wake history, and
network details.

Each section is cached with its own refresh interval and rebuilt in the
background, so the dashboard never waits on a slow command.
"""
import datetime
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")
UID = os.getuid()


def run(cmd, timeout=10, env=None, err=False):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, **(env or {})))
        return (r.stdout + (r.stderr if err else "")).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def plist_file(path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def tilde(p):
    return "~" + p[len(HOME):] if p and p.startswith(HOME) else p


def du(path, timeout=20):
    o = run(["du", "-sk", path], timeout=timeout)
    try:
        return int(o.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def security():
    items = []

    def add(id_, title, ok, status, detail, fix=None, level="warn", known=True):
        items.append({"id": id_, "title": title, "ok": ok if known else None, "status": status if known else "Unknown",
                      "detail": detail, "fix": fix, "level": ("ok" if ok else level) if known else "unknown"})

    fv = run(["fdesetup", "status"])
    add("filevault", "FileVault disk encryption", "On" in fv, "On" if "On" in fv else ("Off" if fv else "Unknown"),
        "Encrypts your disk so a lost or stolen Mac can't be read.",
        "System Settings › Privacy & Security › FileVault", "bad", bool(fv))
    sip = run(["csrutil", "status"])
    add("sip", "System Integrity Protection", "enabled" in sip.lower() and "disabled" not in sip.lower(),
        "Enabled" if "enabled" in sip.lower() else "Disabled", "Stops anything, even admin tools, modifying macOS system files.",
        "Re-enable from Recovery: csrutil enable", "bad", bool(sip))
    gk = run(["spctl", "--status"], err=True)
    add("gatekeeper", "Gatekeeper", "enabled" in gk, "On" if "enabled" in gk else "Off",
        "Only lets apps from the App Store and identified developers open.", "sudo spctl --master-enable", "bad", bool(gk))
    fw_path = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    fw = run([fw_path, "--getglobalstate"]) if os.path.exists(fw_path) else ""
    fw_on = "enabled" in fw.lower() or "state = 1" in fw.lower() or "state = 2" in fw.lower()
    stealth = run([fw_path, "--getstealthmode"]) if os.path.exists(fw_path) else ""
    add("firewall", "Firewall", fw_on, "On" if fw_on else "Off",
        "Blocks unwanted incoming connections." + (" Stealth mode is on." if "enabled" in stealth.lower() or "is on" in stealth.lower() else ""),
        "System Settings › Network › Firewall", "warn", bool(fw))
    au = run(["defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "AutomaticCheckEnabled"])
    sec = run(["defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "CriticalUpdateInstall"])
    add("autoupdate", "Automatic security updates", au != "0" and sec != "0",
        "On" if (au != "0" and sec != "0") else "Off",
        "Installs security responses and system files automatically.",
        "System Settings › General › Software Update › Automatic Updates")
    sl = run(["sysadminctl", "-screenLock", "status"], err=True)
    m = re.search(r"screenLock delay is (\d+) seconds", sl)
    if m or "immediate" in sl.lower():
        delay = int(m.group(1)) if m else 0
        add("screenlock", "Password after sleep / screen saver", delay <= 300,
            "Immediately" if delay == 0 else "After %d s" % delay, "How quickly your Mac asks for a password once it locks.",
            "System Settings › Lock Screen")
    elif "off" in sl.lower():
        add("screenlock", "Password after sleep / screen saver", False, "Off",
            "Anyone can use your Mac after it wakes.", "System Settings › Lock Screen", "bad")
    xp = plist_file("/Library/Apple/System/Library/CoreServices/XProtect.bundle/Contents/Info.plist")
    if xp:
        add("xprotect", "XProtect malware definitions", True, "Version %s" % xp.get("CFBundleShortVersionString", "?"),
            "Apple's built-in malware scanner. Updated silently.")
    # Sharing services, detected from listening sockets (visible without admin rights)
    ns = run(["netstat", "-anv", "-p", "tcp"], timeout=6)
    listening = set()
    for line in ns.splitlines():
        if "LISTEN" in line:
            p = line.split()
            if len(p) > 3:
                port = p[3].rsplit(".", 1)[-1]
                if port.isdigit():
                    listening.add(int(port))
    services = [(22, "Remote Login (SSH)"), (5900, "Screen Sharing"), (3283, "Remote Management"),
                (445, "File Sharing (SMB)"), (548, "File Sharing (AFP)"), (7000, "AirPlay Receiver"),
                (3689, "Media Sharing"), (631, "Printer Sharing")]
    sharing = [{"port": p, "name": n} for p, n in services if p in listening]
    risky = [s for s in sharing if s["port"] in (22, 5900, 3283, 445, 548)]
    add("sharing", "Sharing services", not risky, ", ".join(s["name"] for s in sharing) or "None",
        "Services other devices can connect to. Turn off any you don't use.", "System Settings › General › Sharing", "info")
    fmm = run(["nvram", "-x", "fmm-mobileme-token-FMM"])
    add("findmy", "Find My Mac", bool(fmm), "On" if fmm else "Off / unknown",
        "Lets you locate, lock or erase this Mac if it's lost.", "System Settings › Apple Account › iCloud › Find My Mac", "info")
    known = [i for i in items if i["ok"] is not None]
    score = sum(1 for i in known if i["ok"]) * 100 // max(1, len(known))
    return {"items": items, "score": score, "listening": sorted(listening)[:80], "sharing": sharing}


# ---------------------------------------------------------------------------
# Software: apps, Homebrew, macOS updates, background items
# ---------------------------------------------------------------------------
def _mdls_last_used(paths):
    out = {}
    for i in range(0, len(paths), 40):
        chunk = paths[i:i + 40]
        o = run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", "-nullMarker", "-"] + chunk, timeout=30)
        vals = o.split("\x00") if "\x00" in o else o.splitlines()
        for p, v in zip(chunk, vals):
            v = v.strip()
            if v and v != "-" and v != "(null)":
                try:
                    out[p] = datetime.datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc).timestamp()
                except ValueError:
                    pass
    return out


def _archs(app, info):
    exe = info.get("CFBundleExecutable")
    if not exe:
        return None
    path = os.path.join(app, "Contents", "MacOS", exe)
    o = run(["lipo", "-archs", path], timeout=5)
    return o.split() if o else None


def apps_inventory(sizes=True):
    roots = ["/Applications", os.path.join(HOME, "Applications")]
    paths = []
    for r in roots:
        for p in glob.glob(os.path.join(r, "*.app")) + glob.glob(os.path.join(r, "*", "*.app")):
            if not os.path.islink(p):
                paths.append(p)
    used = _mdls_last_used(paths)
    apps = []
    for p in sorted(set(paths)):
        info = plist_file(os.path.join(p, "Contents", "Info.plist"))
        arch = _archs(p, info)
        try:
            owner_ok = os.access(p, os.W_OK)
        except OSError:
            owner_ok = False
        apps.append({
            "name": os.path.basename(p)[:-4], "path": p, "id": info.get("CFBundleIdentifier"),
            "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion"),
            "size": du(p, timeout=90) if sizes else None, "last_used": used.get(p),
            "appstore": os.path.exists(os.path.join(p, "Contents", "_MASReceipt")),
            "intel_only": bool(arch) and "arm64" not in arch, "archs": arch,
            "apple": (info.get("CFBundleIdentifier") or "").startswith("com.apple."), "writable": owner_ok,
        })
    return apps


def homebrew():
    brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(p)), None)
    if not brew:
        return None
    env = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1"}
    formulae = run([brew, "list", "--formula", "-1"], timeout=20, env=env).split()
    casks = run([brew, "list", "--cask", "-1"], timeout=20, env=env).split()
    outdated = []
    try:
        d = json.loads(run([brew, "outdated", "--json=v2"], timeout=60, env=env) or "{}")
        for f in d.get("formulae", []):
            outdated.append({"name": f["name"], "kind": "formula", "have": ", ".join(f.get("installed_versions", [])),
                             "want": f.get("current_version")})
        for c in d.get("casks", []):
            outdated.append({"name": c["name"], "kind": "cask", "have": c.get("installed_versions"),
                             "want": c.get("current_version")})
    except ValueError:
        pass
    cache = run([brew, "--cache"], timeout=10, env=env)
    services = []
    for line in run([brew, "services", "list"], timeout=20, env=env).splitlines()[1:]:
        p = line.split()
        if len(p) >= 2:
            services.append({"name": p[0], "status": p[1]})
    return {"formulae": len(formulae), "casks": len(casks), "outdated": outdated, "cache": cache,
            "cache_size": du(cache) if cache and os.path.isdir(cache) else None, "services": services,
            "prefix": run([brew, "--prefix"], timeout=10, env=env), "version": run([brew, "--version"], env=env).splitlines()[0:1]}


def macos_updates():
    o = run(["softwareupdate", "-l"], timeout=120, err=True)
    ups = []
    for m in re.finditer(r"\* Label: (.+)\n\s*Title: ([^,]+), Version: ([^,]+), Size: ([^,]+),(.*)", o):
        ups.append({"label": m.group(1).strip(), "title": m.group(2).strip(), "version": m.group(3).strip(),
                    "size": m.group(4).strip(), "restart": "Restart" in m.group(5)})
    return {"updates": ups, "checked": time.time(), "raw": "" if ups else o[-300:]}


def background_items():
    loaded = {}
    for line in run(["launchctl", "list"], timeout=8).splitlines()[1:]:
        p = line.split(None, 2)
        if len(p) == 3:
            loaded[p[2]] = {"pid": None if p[0] == "-" else int(p[0]), "status": p[1]}
    out = []
    for scope, d in (("user", os.path.join(HOME, "Library/LaunchAgents")), ("all users", "/Library/LaunchAgents"),
                     ("system", "/Library/LaunchDaemons")):
        for f in sorted(glob.glob(os.path.join(d, "*.plist"))):
            pl = plist_file(f)
            label = pl.get("Label") or os.path.basename(f)[:-6]
            prog = pl.get("Program") or (pl.get("ProgramArguments") or [None])[0]
            missing = bool(prog) and prog.startswith("/") and not os.path.exists(prog)
            st = loaded.get(label)
            out.append({"label": label, "scope": scope, "file": tilde(f), "program": tilde(prog) if prog else None,
                        "missing": missing, "loaded": st is not None, "running": bool(st and st["pid"]),
                        "run_at_load": bool(pl.get("RunAtLoad")), "keep_alive": bool(pl.get("KeepAlive")),
                        "disabled": bool(pl.get("Disabled")), "removable": scope == "user"})
    login = run(["osascript", "-e", 'tell application "System Events" to get the name of every login item'], timeout=8)
    return {"agents": out, "login_items": [x.strip() for x in login.split(",") if x.strip()] if login else None}


# ---------------------------------------------------------------------------
# Developer
# ---------------------------------------------------------------------------
TOOLS = [("git", ["--version"]), ("node", ["--version"]), ("npm", ["--version"]), ("pnpm", ["--version"]),
         ("yarn", ["--version"]), ("bun", ["--version"]), ("deno", ["--version"]), ("python3", ["--version"]),
         ("uv", ["--version"]), ("pip3", ["--version"]), ("go", ["version"]), ("rustc", ["--version"]),
         ("java", ["-version"]), ("ruby", ["--version"]), ("swift", ["--version"]), ("xcodebuild", ["-version"]),
         ("docker", ["--version"]), ("kubectl", ["version", "--client"]), ("terraform", ["--version"]),
         ("aws", ["--version"]), ("gcloud", ["--version"]), ("az", ["--version"]), ("databricks", ["--version"]),
         ("gh", ["--version"]), ("brew", ["--version"]), ("ollama", ["--version"]), ("claude", ["--version"]),
         ("code", ["--version"]), ("cursor", ["--version"]), ("pod", ["--version"]), ("flutter", ["--version"]),
         ("kestra", ["--version"])]
PATHS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".cargo/bin"),
         os.path.join(HOME, ".bun/bin"), os.path.join(HOME, "go/bin")]


def dev_tools():
    env_path = os.pathsep.join([os.environ.get("PATH", "")] + PATHS)
    out = []
    for name, args in TOOLS:
        exe = shutil.which(name, path=env_path)
        if not exe:
            continue
        v = run([exe] + args, timeout=6, err=True, env={"PATH": env_path})
        line = next((l for l in v.splitlines() if re.search(r"\d+\.\d+", l)), v.splitlines()[0] if v else "")
        m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?)", line)
        out.append({"name": name, "version": m.group(1) if m else line[:40], "path": tilde(os.path.realpath(exe))})
    return out


def git_repos(roots=None, max_depth=4, limit=150):
    roots = roots or [os.path.join(HOME, d) for d in ("projects", "Projects", "Developer", "code", "src", "dev", "Desktop",
                                                      "Documents/GitHub", "repos", "work")]
    found = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        base = root.rstrip("/").count("/")
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.count("/") - base
            if ".git" in dirnames:
                rp = os.path.realpath(dirpath)
                if rp not in seen:
                    seen.add(rp)
                    found.append(dirpath)
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "Library", "venv", "build", "dist", "target", "Pods")]
            if depth >= max_depth or len(found) >= limit:
                dirnames[:] = []
    repos = []
    for d in found:
        g = lambda *a: run(["git", "-C", d] + list(a), timeout=8)
        branch = g("rev-parse", "--abbrev-ref", "HEAD")
        dirty = [l for l in g("status", "--porcelain").splitlines() if l.strip()]
        ab = g("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
        behind = ahead = None
        if ab and "\t" in ab:
            behind, ahead = (int(x) for x in ab.split("\t"))
        last = g("log", "-1", "--format=%ct|%s")
        ts, msg = (last.split("|", 1) + [""])[:2] if last else ("", "")
        remote = g("remote", "get-url", "origin")
        stashes = len(g("stash", "list").splitlines())
        repos.append({"path": tilde(d), "name": os.path.basename(d), "branch": branch, "dirty": len(dirty),
                      "untracked": sum(1 for l in dirty if l.startswith("??")), "ahead": ahead, "behind": behind,
                      "no_upstream": ab == "", "last_commit": int(ts) if ts.isdigit() else None, "last_msg": msg[:80],
                      "remote": re.sub(r"https://[^@/]+@", "https://", remote)[:100], "stashes": stashes})
    repos.sort(key=lambda r: -(r["last_commit"] or 0))
    return repos


def docker_info():
    docker = shutil.which("docker") or ("/usr/local/bin/docker" if os.path.exists("/usr/local/bin/docker") else None)
    if not docker:
        return None
    df = run([docker, "system", "df", "--format", "{{json .}}"], timeout=10)
    if not df:
        return {"running": False}
    usage = []
    for line in df.splitlines():
        try:
            usage.append(json.loads(line))
        except ValueError:
            pass
    ps = []
    for line in run([docker, "ps", "-a", "--format", "{{json .}}"], timeout=10).splitlines():
        try:
            c = json.loads(line)
            ps.append({"name": c.get("Names"), "image": c.get("Image"), "status": c.get("Status"), "state": c.get("State"),
                       "ports": c.get("Ports")})
        except ValueError:
            pass
    return {"running": True, "usage": usage, "containers": ps[:50]}


def ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            d = json.load(r)
        return [{"name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at", "")[:19],
                 "family": (m.get("details") or {}).get("family"), "params": (m.get("details") or {}).get("parameter_size"),
                 "quant": (m.get("details") or {}).get("quantization_level")} for m in d.get("models", [])]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hardware inventory (system_profiler, everything)
# ---------------------------------------------------------------------------
SP_TYPES = ["SPHardwareDataType", "SPSoftwareDataType", "SPNVMeDataType", "SPStorageDataType", "SPMemoryDataType",
            "SPDisplaysDataType", "SPPowerDataType", "SPBluetoothDataType", "SPAudioDataType", "SPCameraDataType",
            "SPThunderboltDataType", "SPUSBDataType", "SPUSBHostDataType", "SPNetworkDataType", "SPPrintersDataType",
            "SPSecureElementDataType", "SPDiagnosticsDataType"]
REDACT = re.compile(r"serial|uuid|udid|hardware_uuid|provisioning|platform_uuid|mac_address|ethernet.*address|address$", re.I)


def hardware_inventory():
    o = run(["system_profiler", "-json", "-detailLevel", "basic"] + SP_TYPES, timeout=90)
    try:
        d = json.loads(o)
    except ValueError:
        return None

    def clean(x):
        if isinstance(x, dict):
            return {k: ("•••" + str(v)[-4:] if REDACT.search(k) and isinstance(v, str) and len(v) > 6 else clean(v))
                    for k, v in x.items()}
        if isinstance(x, list):
            return [clean(i) for i in x]
        return x
    return {k: clean(v) for k, v in d.items() if v}


# ---------------------------------------------------------------------------
# Logs: crashes, panics, sleep/wake, reboots
# ---------------------------------------------------------------------------
def logs():
    crashes = []
    for d in (os.path.join(HOME, "Library/Logs/DiagnosticReports"), "/Library/Logs/DiagnosticReports"):
        for f in glob.glob(os.path.join(d, "*")):
            base = os.path.basename(f)
            if not re.search(r"\.(ips|crash|panic|diag|spin|hang|cpu_resource)", base):
                continue
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            m = re.match(r"(.+?)[-_](\d{4}-\d{2}-\d{2})", base)
            kind = "panic" if "panic" in base.lower() else "hang" if re.search(r"hang|spin", base) else \
                "cpu" if "cpu_resource" in base else "diag" if "diag" in base else "crash"
            crashes.append({"app": m.group(1) if m else base.split(".")[0], "file": tilde(f), "ts": mt, "kind": kind})
    crashes.sort(key=lambda c: -c["ts"])
    by_app = {}
    month = time.time() - 30 * 86400
    for c in crashes:
        if c["ts"] > month:
            by_app[c["app"]] = by_app.get(c["app"], 0) + 1
    # sleep / wake history (last ~60 events)
    pm = run(["sh", "-c", "pmset -g log | grep -E '\\s(Sleep|Wake|DarkWake)\\s' | tail -80"], timeout=25)
    sleep = []
    for line in pm.splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [+-]\d{4}\s+(Sleep|Wake|DarkWake)\s+(.*?)\s{2,}(.*)", line)
        if m:
            sleep.append({"ts": m.group(1), "kind": m.group(2), "reason": re.sub(r"\s+", " ", m.group(4))[:140]})
    sleep.reverse()
    boots = []
    for line in run(["last", "reboot"], timeout=5).splitlines():
        m = re.search(r"reboot\s+~\s+(.+?)\s*$", line)
        if m:
            boots.append(m.group(1))
    shutdown = []
    for line in run(["last", "shutdown"], timeout=5).splitlines():
        m = re.search(r"shutdown\s+~\s+(.+?)\s*$", line)
        if m:
            shutdown.append(m.group(1))
    return {"crashes": crashes[:150], "crash_counts": sorted(by_app.items(), key=lambda kv: -kv[1])[:15],
            "sleep": sleep[:60], "reboots": boots[:15], "shutdowns": shutdown[:15]}


# ---------------------------------------------------------------------------
# Network extras
# ---------------------------------------------------------------------------
def network_extras(public_ip=False):
    dns = []
    for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", run(["scutil", "--dns"], timeout=5)):
        if m.group(1) not in dns:
            dns.append(m.group(1))
    conns = {}
    remotes = {}
    for line in run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fcn"], timeout=8).splitlines():
        if line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n") and "->" in line:
            conns[cmd] = conns.get(cmd, 0) + 1
            host = line.split("->", 1)[1].rsplit(":", 1)[0].strip("[]")
            remotes[host] = remotes.get(host, 0) + 1
    vpn = []
    for line in run(["scutil", "--nc", "list"], timeout=5).splitlines():
        m = re.search(r"\((Connected|Disconnected|Connecting)\).*?\"(.+?)\"", line)
        if m:
            vpn.append({"name": m.group(2), "status": m.group(1)})
    pub = None
    if public_ip:
        try:
            with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=4) as r:
                pub = json.load(r).get("ip")
        except Exception:
            pub = None
    proxy = run(["scutil", "--proxy"], timeout=4)
    return {"dns": dns[:8], "connections": sorted(conns.items(), key=lambda kv: -kv[1])[:20],
            "total_connections": sum(conns.values()), "top_remotes": sorted(remotes.items(), key=lambda kv: -kv[1])[:12],
            "vpn": vpn, "public_ip": pub, "proxy_on": bool(re.search(r"(HTTP|HTTPS|SOCKS)Enable : 1", proxy)),
            "hostname": run(["scutil", "--get", "LocalHostName"], timeout=3)}


# ---------------------------------------------------------------------------
# Cache + background refresh
# ---------------------------------------------------------------------------
SECTIONS = {
    # name: (function, refresh seconds)
    "security": (security, 600),
    "apps": (apps_inventory, 3600),
    "brew": (homebrew, 3600),
    "updates": (macos_updates, 6 * 3600),
    "background": (background_items, 900),
    "tools": (dev_tools, 3600),
    "repos": (git_repos, 300),
    "docker": (docker_info, 120),
    "ollama": (ollama_models, 300),
    "hardware": (hardware_inventory, 6 * 3600),
    "logs": (logs, 600),
    "netx": (network_extras, 60),
}


class Inventory:
    def __init__(self, on_update=None, settings=None):
        self.data = {}
        self.when = {}
        self.busy = set()
        self.lock = threading.Lock()
        self.on_update = on_update
        self.settings = settings or {}
        self.queue = []
        self.cv = threading.Condition()

    def get(self, names):
        with self.lock:
            res = {n: {"data": self.data.get(n), "updated": self.when.get(n), "busy": n in self.busy} for n in names}
        stale = [n for n in names if n in SECTIONS and time.time() - self.when.get(n, 0) > SECTIONS[n][1]]
        if stale:
            self.request(stale)
        return res

    def request(self, names, force=False):
        with self.cv:
            for n in names:
                if n in SECTIONS and n not in self.queue and n not in self.busy:
                    if force or time.time() - self.when.get(n, 0) > SECTIONS[n][1]:
                        self.queue.append(n)
            self.cv.notify_all()

    def worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                n = self.queue.pop(0)
                self.busy.add(n)
            if self.on_update:
                self.on_update(n, None, True)
            fn = SECTIONS[n][0]
            try:
                val = fn(public_ip=self.settings.get("public_ip", False)) if n == "netx" else fn()
            except Exception as e:
                val = {"error": str(e)}
            with self.lock:
                self.data[n], self.when[n] = val, time.time()
                self.busy.discard(n)
            if self.on_update:
                self.on_update(n, val, False)

    def start(self, workers=2):
        for _ in range(workers):
            threading.Thread(target=self.worker, daemon=True).start()
        # warm the cheap/important ones first
        self.request(["security", "netx", "tools", "docker", "ollama", "background", "logs", "repos", "brew", "apps", "hardware", "updates"])


if __name__ == "__main__":
    which = sys.argv[1:] or list(SECTIONS)
    for n in which:
        t = time.time()
        v = SECTIONS[n][0]()
        print("== %s (%.1fs)" % (n, time.time() - t))
        print(json.dumps(v, indent=1, default=str)[:3000])
