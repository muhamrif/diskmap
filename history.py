"""
history — keeps a rolling record of the live stats in SQLite so the dashboard
can chart the last hour, day, week or month.

Metrics are written every 10 s, the busiest apps every 60 s. Data older than
`retain_days` is dropped automatically.
"""
import os
import sqlite3
import threading
import time

METRICS = ["cpu", "mem_used", "mem_pct", "swap", "gpu", "temp", "power", "cpu_w", "gpu_w",
           "net_down", "net_up", "io_read", "io_write", "battery", "disk_free", "ping", "bat_plugged", "bat_w"]
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


class History:
    def __init__(self, path, retain_days=30):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.retain = retain_days * 86400
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS m (ts INTEGER PRIMARY KEY, %s)" % ", ".join("%s REAL" % c for c in METRICS))
        self.db.execute("CREATE TABLE IF NOT EXISTS apps (ts INTEGER, name TEXT, cpu REAL, mem REAL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS apps_ts ON apps(ts)")
        if "energy" not in [r[1] for r in self.db.execute("PRAGMA table_info(apps)")]:
            self.db.execute("ALTER TABLE apps ADD COLUMN energy REAL")
        self.db.execute("CREATE INDEX IF NOT EXISTS apps_name ON apps(name, ts)")
        have = [r[1] for r in self.db.execute("PRAGMA table_info(m)")]
        for c in METRICS:
            if c not in have:
                self.db.execute("ALTER TABLE m ADD COLUMN %s REAL" % c)
        self.db.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER, kind TEXT, text TEXT)")
        if "num" not in [r[1] for r in self.db.execute("PRAGMA table_info(events)")]:
            self.db.execute("ALTER TABLE events ADD COLUMN num REAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS battery_daily (day TEXT PRIMARY KEY, ts INTEGER, health REAL, cycles INTEGER, "
                        "max_mah INTEGER, design_mah INTEGER, temp REAL)")
        self.db.commit()
        self.repair()
        self.acc = []           # samples since the last write
        self.app_acc = {}       # name -> [cpu_sum, mem_sum, n]
        self.last_write = 0
        self.last_app_write = 0
        self.last_prune = 0

    def repair(self, ncpu=None):
        """Drop impossible values recorded by older versions (spikes across sleep, bad CPU parsing)."""
        cap_cpu = 100.0 * (ncpu or os.cpu_count() or 16)
        with self.lock:
            for col, lim in (("io_read", 20e9), ("io_write", 20e9), ("net_down", 12e9), ("net_up", 12e9),
                             ("cpu_w", 400), ("gpu_w", 400), ("power", 400)):
                self.db.execute("UPDATE m SET %s = NULL WHERE %s > ?" % (col, col), (lim,))
            self.db.execute("DELETE FROM apps WHERE cpu > ?", (cap_cpu,))
            self.db.commit()

    def add(self, snap):
        """Feed every live sample; aggregates and writes on its own schedule."""
        hw = snap.get("hw") or {}
        pw = hw.get("power") or {}
        row = {
            "cpu": snap["cpu"]["total"], "mem_used": snap["mem"]["used"],
            "mem_pct": 100 * snap["mem"]["used"] / max(1, snap["mem"]["total"]), "swap": snap["mem"]["swap"],
            "gpu": (snap.get("gpu") or {}).get("util"), "temp": (hw.get("temps") or {}).get("cpu_max"),
            "power": hw.get("sys_power"), "cpu_w": pw.get("cpu"), "gpu_w": pw.get("gpu"),
            "net_down": (hw.get("net") or {}).get("down"), "net_up": (hw.get("net") or {}).get("up"),
            "io_read": (hw.get("io") or {}).get("read"), "io_write": (hw.get("io") or {}).get("write"),
            "battery": (hw.get("battery") or {}).get("percent"), "disk_free": snap["disk"]["free"],
            "ping": (snap.get("hw_slow") or {}).get("ping_internet"),
            "bat_plugged": None if not (hw.get("battery") or {}) else (1.0 if hw["battery"].get("plugged") else 0.0),
            "bat_w": (hw.get("battery") or {}).get("watts"),
        }
        bat = hw.get("battery") or {}
        if bat.get("cycles") is not None and time.time() - getattr(self, "_last_bat_day", 0) > 3600:
            self._last_bat_day = time.time()
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO battery_daily VALUES (?,?,?,?,?,?,?)",
                                (time.strftime("%Y-%m-%d"), int(time.time()), bat.get("health"), bat.get("cycles"),
                                 bat.get("max_mah"), bat.get("design_mah"), bat.get("temp")))
                self.db.commit()
        self.acc.append(row)
        for a in snap.get("apps", []):
            s = self.app_acc.setdefault(a["name"], [0.0, 0.0, 0, 0.0])
            s[0] += a["cpu"]
            s[1] += a["mem"]
            s[2] += 1
            s[3] += a.get("power") or 0.0
        now = time.time()
        if now - self.last_write >= 10:
            self._write(int(now))
        if now - self.last_app_write >= 60:
            self._write_apps(int(now))
        if now - self.last_prune > 3600:
            self._prune(now)

    def _write(self, ts):
        rows, self.acc = self.acc, []
        self.last_write = ts
        if not rows:
            return
        avg = {}
        for c in METRICS:
            vals = [r[c] for r in rows if r.get(c) is not None]
            avg[c] = sum(vals) / len(vals) if vals else None
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO m (ts, %s) VALUES (?, %s)" % (", ".join(METRICS), ", ".join("?" * len(METRICS))),
                            [ts] + [avg[c] for c in METRICS])
            self.db.commit()

    def _write_apps(self, ts):
        acc, self.app_acc = self.app_acc, {}
        self.last_app_write = ts
        rows = [(ts, k, v[0] / v[2], v[1] / v[2], v[3] / v[2]) for k, v in acc.items() if v[2]]
        # keep anything doing real work, plus the biggest memory users, so each app gets its own history
        keep = [r for r in rows if r[2] >= 0.5 or r[3] >= 150e6 or r[4] >= 1]
        keep = sorted(keep, key=lambda r: -(r[2] + r[3] / 1e8))[:60]
        with self.lock:
            self.db.executemany("INSERT INTO apps (ts, name, cpu, mem, energy) VALUES (?,?,?,?,?)", keep)
            self.db.commit()

    def event(self, kind, text, num=None):
        with self.lock:
            self.db.execute("INSERT INTO events (ts, kind, text, num) VALUES (?,?,?,?)", (int(time.time()), kind, text, num))
            self.db.commit()

    def _prune(self, now):
        self.last_prune = now
        cut = int(now - self.retain)
        with self.lock:
            self.db.execute("DELETE FROM m WHERE ts < ?", (cut,))
            self.db.execute("DELETE FROM apps WHERE ts < ?", (cut,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
            self.db.commit()

    def query(self, rng="1h"):
        span = RANGES.get(rng, 3600)
        bucket = max(10, span // 360)  # ~360 points per chart
        since = int(time.time() - span)
        cols = ", ".join("AVG(%s), MAX(%s)" % (c, c) for c in METRICS)
        with self.lock:
            rows = self.db.execute("SELECT (ts / ?) * ? AS b, %s FROM m WHERE ts >= ? GROUP BY b ORDER BY b" % cols,
                                   (bucket, bucket, since)).fetchall()
            apps = self.db.execute("SELECT name, SUM(cpu) / ?, MAX(cpu), AVG(mem), MAX(mem), COUNT(*), SUM(COALESCE(energy, 0)) / ? "
                                   "FROM apps WHERE ts >= ? GROUP BY name ORDER BY SUM(cpu) DESC LIMIT 30",
                                   (max(1, span / 60), max(1, span / 60), since)).fetchall()
            events = self.db.execute("SELECT ts, kind, text FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT 100",
                                     (since,)).fetchall()
            first = self.db.execute("SELECT MIN(ts) FROM m").fetchone()[0]
        # insert an empty point wherever the Mac was asleep (or the app closed) so charts show a gap
        gapped, prev = [], None
        blank = (None,) * (2 * len(METRICS))
        for r in rows:
            if prev is not None and r[0] - prev > max(3 * bucket, 60):
                gapped.append((prev + bucket,) + blank)
            gapped.append(r)
            prev = r[0]
        rows = gapped
        series = {"t": [r[0] for r in rows]}
        for i, c in enumerate(METRICS):
            series[c] = [r[1 + 2 * i] for r in rows]
            series[c + "_max"] = [r[2 + 2 * i] for r in rows]
        return {"range": rng, "bucket": bucket, "since": since, "first": first, "series": series,
                "apps": [{"name": a[0], "cpu_avg": a[1], "cpu_max": a[2], "mem_avg": a[3], "mem_max": a[4], "minutes": a[5],
                          "energy": a[6]} for a in apps],
                "events": [{"ts": e[0], "kind": e[1], "text": e[2]} for e in events]}

    def app_query(self, name, rng="24h"):
        span = RANGES.get(rng, 86400)
        bucket = max(60, span // 240)
        since = int(time.time() - span)
        with self.lock:
            rows = self.db.execute("SELECT (ts / ?) * ? AS b, AVG(cpu), AVG(mem), AVG(energy) FROM apps WHERE name = ? AND ts >= ? "
                                   "GROUP BY b ORDER BY b", (bucket, bucket, name, since)).fetchall()
            st = self.db.execute("SELECT AVG(cpu), MAX(cpu), AVG(mem), MAX(mem), AVG(energy), COUNT(*) FROM apps WHERE name = ? AND ts >= ?",
                                 (name, since)).fetchone()
        t, cpu, mem, en = [], [], [], []
        prev = None
        for r in rows:  # gaps = app not running (or Mac asleep)
            if prev is not None and r[0] - prev > 2.5 * bucket:
                t.append(prev + bucket); cpu.append(None); mem.append(None); en.append(None)
            t.append(r[0]); cpu.append(r[1]); mem.append(r[2]); en.append(r[3])
            prev = r[0]
        return {"name": name, "range": rng, "t": t, "cpu": cpu, "mem": mem, "energy": en,
                "stats": {"cpu_avg": st[0], "cpu_max": st[1], "mem_avg": st[2], "mem_max": st[3], "energy_avg": st[4], "minutes": st[5]}}

    # ---------- memory growth ----------
    def mem_since(self, name, since):
        """Minute samples of one app's memory since `since` (for growth detection)."""
        with self.lock:
            return self.db.execute("SELECT ts, mem FROM apps WHERE name = ? AND ts >= ? ORDER BY ts", (name, int(since))).fetchall()

    # ---------- battery report ----------
    def battery_report(self, days=60):
        since = int(time.time() - days * 86400)
        with self.lock:
            daily = self.db.execute("SELECT day, health, cycles, max_mah, design_mah, temp FROM battery_daily WHERE ts >= ? ORDER BY day",
                                    (since,)).fetchall()
            rows = self.db.execute("SELECT ts, battery, bat_plugged, bat_w FROM m WHERE ts >= ? AND battery IS NOT NULL ORDER BY ts",
                                   (int(time.time() - 14 * 86400),)).fetchall()
        # discharge sessions: stretches on battery, split at gaps (sleep) longer than 20 min
        sessions, cur = [], None
        for ts, pct, plugged, w in rows:
            on_bat = plugged is not None and plugged < 0.5
            if on_bat and cur and ts - cur["last"] <= 1200:
                cur["last"], cur["end_pct"] = ts, pct
                cur["w"].append(abs(w or 0))
            else:
                if cur:
                    sessions.append(cur)
                cur = {"start": ts, "last": ts, "start_pct": pct, "end_pct": pct, "w": [abs(w or 0)]} if on_bat else None
        if cur:
            sessions.append(cur)
        out = []
        for ss in sessions:
            hours = (ss["last"] - ss["start"]) / 3600
            used = (ss["start_pct"] or 0) - (ss["end_pct"] or 0)
            if hours < 0.25 or used < 2:
                continue
            rate = used / hours
            out.append({"start": ss["start"], "end": ss["last"], "hours": round(hours, 2), "from": ss["start_pct"], "to": ss["end_pct"],
                        "rate": round(rate, 1), "full_hours": round(100 / rate, 1) if rate > 0 else None,
                        "avg_w": round(sum(ss["w"]) / len(ss["w"]), 1) if ss["w"] else None})
        out.reverse()
        # energy hogs while on battery
        hogs = []
        if out:
            clauses = " OR ".join("(ts BETWEEN %d AND %d)" % (x["start"], x["end"]) for x in out[:20])
            with self.lock:
                hogs = self.db.execute("SELECT name, SUM(COALESCE(energy,0)) e, AVG(cpu) FROM apps WHERE %s GROUP BY name ORDER BY e DESC LIMIT 8"
                                       % clauses).fetchall()
        cyc = [d for d in daily if d[2] is not None]
        per_week = None
        if len(cyc) >= 2:
            span_days = max(1, (time.mktime(time.strptime(cyc[-1][0], "%Y-%m-%d")) - time.mktime(time.strptime(cyc[0][0], "%Y-%m-%d"))) / 86400)
            per_week = round((cyc[-1][2] - cyc[0][2]) / span_days * 7, 1)
        rates = [x["rate"] for x in out if x["hours"] >= 0.5]
        return {"daily": [{"day": d[0], "health": d[1], "cycles": d[2], "max_mah": d[3], "design_mah": d[4], "temp": d[5]} for d in daily],
                "sessions": out[:30], "cycles_per_week": per_week,
                "typical_rate": round(sorted(rates)[len(rates) // 2], 1) if rates else None,
                "typical_full_hours": round(100 / sorted(rates)[len(rates) // 2], 1) if rates else None,
                "hogs": [{"name": h[0], "energy": h[1], "cpu": h[2]} for h in hogs]}

    # ---------- weekly digest ----------
    def digest(self, days=7):
        now = time.time()
        since = int(now - days * 86400)
        with self.lock:
            agg = self.db.execute("SELECT COUNT(*), AVG(cpu), MAX(cpu), AVG(mem_pct), MAX(swap), AVG(temp), MAX(temp), AVG(power), "
                                  "SUM(net_down) * 10, SUM(net_up) * 10, SUM(io_write) * 10, MIN(ts) FROM m WHERE ts >= ?", (since,)).fetchone()
            first_free = self.db.execute("SELECT disk_free FROM m WHERE ts >= ? AND disk_free IS NOT NULL ORDER BY ts LIMIT 1", (since,)).fetchone()
            last_free = self.db.execute("SELECT disk_free FROM m WHERE disk_free IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
            apps = self.db.execute("SELECT name, SUM(cpu) / ?, AVG(mem), MAX(mem), SUM(COALESCE(energy,0)) / ? FROM apps WHERE ts >= ? "
                                   "GROUP BY name ORDER BY SUM(cpu) DESC LIMIT 5", (days * 1440, days * 1440, since)).fetchall()
            mem_apps = self.db.execute("SELECT name, AVG(mem) FROM apps WHERE ts >= ? GROUP BY name ORDER BY AVG(mem) DESC LIMIT 5", (since,)).fetchall()
            ev = self.db.execute("SELECT kind, COUNT(*), SUM(COALESCE(num,0)) FROM events WHERE ts >= ? GROUP BY kind", (since,)).fetchall()
            hot = self.db.execute("SELECT COUNT(*) FROM m WHERE ts >= ? AND temp >= 90", (since,)).fetchone()[0]
        samples = agg[0] or 0
        evd = {k: {"count": c, "num": n} for k, c, n in ev}
        freed = sum(v["num"] for k, v in evd.items() if k in ("clean", "trash", "autopilot") and v["num"])
        return {"days": days, "since": since, "samples": samples, "hours_on": round(samples * 10 / 3600, 1),
                "cpu_avg": agg[1], "cpu_max": agg[2], "mem_avg": agg[3], "swap_max": agg[4], "temp_avg": agg[5], "temp_max": agg[6],
                "power_avg": agg[7], "net_down": agg[8], "net_up": agg[9], "disk_written": agg[10],
                "free_change": (last_free[0] - first_free[0]) if first_free and last_free else None,
                "freed": freed, "hot_minutes": round(hot * 10 / 60),
                "top_cpu": [{"name": a[0], "cpu": a[1], "mem": a[2], "mem_max": a[3], "energy": a[4]} for a in apps],
                "top_mem": [{"name": a[0], "mem": a[1]} for a in mem_apps],
                "events": evd}

    def size(self):
        try:
            return sum(os.path.getsize(self.path + s) for s in ("", "-wal") if os.path.exists(self.path + s))
        except OSError:
            return 0
