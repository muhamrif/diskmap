#!/usr/bin/env python3
"""
macstats — hardware sensors and system stats for macOS, no sudo needed.

    python3 macstats.py        # self-test: prints everything it can read on this Mac

Temperatures come from the IOKit HID sensor hub, fans and system power from the
SMC, per-component power (CPU / GPU / ANE / DRAM) from IOReport — the same
sources tools like Stats and macmon use. Every source is optional: if one isn't
available on a given Mac or macOS version it is simply left out.
"""
import collections
import ctypes
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_int, c_int32, c_int64, c_size_t, c_uint, c_uint32, \
    c_uint64, c_void_p

MAC = sys.platform == "darwin"


def run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def run_plist(cmd, timeout=8):
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
        return plistlib.loads(raw) if raw.strip() else None
    except Exception:
        return None


def sysctl(name):
    return run(["sysctl", "-n", name]).strip()


# ---------------------------------------------------------------------------
# CoreFoundation / IOKit plumbing (ctypes)
# ---------------------------------------------------------------------------
CF = IOKit = LIBC = None
UTF8 = 0x08000100
if MAC:
    try:
        CF = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        IOKit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        LIBC = ctypes.cdll.LoadLibrary("/usr/lib/libSystem.dylib")
        CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        CF.CFStringCreateWithCString.restype = c_void_p
        CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int64, c_uint32]
        CF.CFStringGetCString.restype = c_bool
        CF.CFNumberCreate.argtypes = [c_void_p, c_int64, c_void_p]
        CF.CFNumberCreate.restype = c_void_p
        CF.CFDictionaryCreate.argtypes = [c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_int64, c_void_p, c_void_p]
        CF.CFDictionaryCreate.restype = c_void_p
        CF.CFDictionaryCreateMutableCopy.argtypes = [c_void_p, c_int64, c_void_p]
        CF.CFDictionaryCreateMutableCopy.restype = c_void_p
        CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        CF.CFDictionaryGetValue.restype = c_void_p
        CF.CFDictionaryGetCount.argtypes = [c_void_p]
        CF.CFDictionaryGetCount.restype = c_int64
        CF.CFArrayGetCount.argtypes = [c_void_p]
        CF.CFArrayGetCount.restype = c_int64
        CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int64]
        CF.CFArrayGetValueAtIndex.restype = c_void_p
        CF.CFRelease.argtypes = [c_void_p]
        CF.CFRelease.restype = None
    except OSError:
        CF = IOKit = LIBC = None

_cfstr_cache = {}


def cfstr(s):
    if s not in _cfstr_cache:
        _cfstr_cache[s] = CF.CFStringCreateWithCString(None, s.encode(), UTF8)
    return _cfstr_cache[s]


def pystr(ref):
    if not ref:
        return None
    buf = ctypes.create_string_buffer(512)
    return buf.value.decode("utf-8", "replace") if CF.CFStringGetCString(ref, buf, 512, UTF8) else None


# ---------------------------------------------------------------------------
# Temperatures — IOHIDEventSystem (works on Apple Silicon without root)
# ---------------------------------------------------------------------------
class HIDTemps:
    EVENT_TEMPERATURE = 15

    def __init__(self):
        f = IOKit
        f.IOHIDEventSystemClientCreate.argtypes = [c_void_p]
        f.IOHIDEventSystemClientCreate.restype = c_void_p
        f.IOHIDEventSystemClientSetMatching.argtypes = [c_void_p, c_void_p]
        f.IOHIDEventSystemClientSetMatching.restype = c_int
        f.IOHIDEventSystemClientCopyServices.argtypes = [c_void_p]
        f.IOHIDEventSystemClientCopyServices.restype = c_void_p
        f.IOHIDServiceClientCopyProperty.argtypes = [c_void_p, c_void_p]
        f.IOHIDServiceClientCopyProperty.restype = c_void_p
        f.IOHIDServiceClientCopyEvent.argtypes = [c_void_p, c_int64, c_int32, c_int64]
        f.IOHIDServiceClientCopyEvent.restype = c_void_p
        f.IOHIDEventGetFloatValue.argtypes = [c_void_p, c_int32]
        f.IOHIDEventGetFloatValue.restype = c_double
        page, usage = c_int32(0xFF00), c_int32(5)
        keys = (c_void_p * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
        vals = (c_void_p * 2)(CF.CFNumberCreate(None, 3, byref(page)), CF.CFNumberCreate(None, 3, byref(usage)))
        kcb = ctypes.addressof(ctypes.c_char.in_dll(CF, "kCFTypeDictionaryKeyCallBacks"))
        vcb = ctypes.addressof(ctypes.c_char.in_dll(CF, "kCFTypeDictionaryValueCallBacks"))
        match = CF.CFDictionaryCreate(None, keys, vals, 2, kcb, vcb)
        self.client = f.IOHIDEventSystemClientCreate(None)
        if not self.client:
            raise OSError("no HID event system")
        f.IOHIDEventSystemClientSetMatching(self.client, match)
        services = f.IOHIDEventSystemClientCopyServices(self.client)
        if not services:
            raise OSError("no temperature sensors")
        self.services = services
        self.items = []
        for i in range(CF.CFArrayGetCount(services)):
            s = CF.CFArrayGetValueAtIndex(services, i)
            name = pystr(f.IOHIDServiceClientCopyProperty(s, cfstr("Product"))) or "sensor %d" % i
            self.items.append((name.strip(), s))
        if not self.items:
            raise OSError("no temperature sensors")

    def read(self):
        vals = collections.defaultdict(list)
        for name, s in self.items:
            ev = IOKit.IOHIDServiceClientCopyEvent(s, self.EVENT_TEMPERATURE, 0, 0)
            if not ev:
                continue
            v = IOKit.IOHIDEventGetFloatValue(ev, self.EVENT_TEMPERATURE << 16)
            CF.CFRelease(ev)
            if -30 < v < 150:
                vals[name].append(v)
        return {k: round(sum(v) / len(v), 1) for k, v in vals.items()}


def classify_sensor(name):
    n = name.lower()
    if "tdie" in n or "cpu" in n or n.startswith("pmu tp") or "soc" in n:
        return "CPU / SoC"
    if "gpu" in n or n.startswith("pmu tg"):
        return "GPU"
    if "nand" in n or "ssd" in n:
        return "SSD"
    if "battery" in n or "gas gauge" in n:
        return "Battery"
    if "tcal" in n:
        return "Calibration"
    if n.startswith("pmu"):
        return "SoC (other)"
    return "Other"


# ---------------------------------------------------------------------------
# SMC — fans and system power
# ---------------------------------------------------------------------------
class SMC:
    def __init__(self):
        f = IOKit
        f.IOServiceMatching.argtypes = [c_char_p]
        f.IOServiceMatching.restype = c_void_p
        f.IOServiceGetMatchingService.argtypes = [c_uint32, c_void_p]
        f.IOServiceGetMatchingService.restype = c_uint32
        f.IOServiceOpen.argtypes = [c_uint32, c_uint32, c_uint32, POINTER(c_uint32)]
        f.IOServiceOpen.restype = c_int
        f.IOConnectCallStructMethod.argtypes = [c_uint32, c_uint32, c_void_p, c_size_t, c_void_p, POINTER(c_size_t)]
        f.IOConnectCallStructMethod.restype = c_int
        svc = f.IOServiceGetMatchingService(0, f.IOServiceMatching(b"AppleSMC"))
        if not svc:
            raise OSError("no AppleSMC")
        task = c_uint32.in_dll(LIBC, "mach_task_self_").value
        self.conn = c_uint32()
        if f.IOServiceOpen(svc, task, 0, byref(self.conn)) != 0:
            raise OSError("can't open SMC")
        self.info = {}

    def _call(self, buf):
        out = ctypes.create_string_buffer(80)
        n = c_size_t(80)
        if IOKit.IOConnectCallStructMethod(self.conn.value, 2, buf, 80, out, byref(n)) != 0:
            return None
        raw = out.raw
        return raw if raw[40] == 0 else None

    def key_info(self, key):
        if key not in self.info:
            b = ctypes.create_string_buffer(80)
            struct.pack_into("<I", b, 0, int.from_bytes(key.encode("latin1"), "big"))
            struct.pack_into("B", b, 42, 9)  # kSMCGetKeyInfo
            o = self._call(b)
            if o:
                size, typ = struct.unpack_from("<II", o, 28)
                self.info[key] = (size, typ.to_bytes(4, "big").decode("latin1"))
            else:
                self.info[key] = None
        return self.info[key]

    def read(self, key):
        inf = self.key_info(key)
        if not inf or not 0 < inf[0] <= 32:
            return None
        size, typ = inf
        b = ctypes.create_string_buffer(80)
        struct.pack_into("<I", b, 0, int.from_bytes(key.encode("latin1"), "big"))
        struct.pack_into("<I", b, 28, size)
        struct.pack_into("B", b, 42, 5)  # kSMCReadKey
        o = self._call(b)
        if not o:
            return None
        d = o[48:48 + size]
        try:
            if typ == "flt ":
                return struct.unpack("<f", d[:4])[0]
            if typ == "ui8 " or typ == "flag":
                return d[0]
            if typ == "ui16":
                return struct.unpack(">H", d[:2])[0]
            if typ == "ui32":
                return struct.unpack(">I", d[:4])[0]
            if typ == "si8 ":
                return struct.unpack("b", d[:1])[0]
            if typ == "si16":
                return struct.unpack(">h", d[:2])[0]
            if typ == "sp78":
                return struct.unpack(">h", d[:2])[0] / 256.0
            if typ == "fpe2":
                return struct.unpack(">H", d[:2])[0] / 4.0
        except struct.error:
            return None
        return None

    def fans(self):
        n = self.read("FNum")
        if not n:
            return []
        out = []
        for i in range(int(n)):
            rpm = self.read("F%dAc" % i)
            if rpm is None:
                continue
            mn, mx = self.read("F%dMn" % i), self.read("F%dMx" % i)
            out.append({"id": i, "rpm": round(rpm), "min": round(mn or 0), "max": round(mx or 0)})
        return out

    def power(self):
        res = {}
        for key, label in (("PSTR", "system"), ("PDTR", "adapter_in")):
            v = self.read(key)
            if v is not None and 0 <= v < 1000:
                res[label] = round(v, 2)
        return res


# ---------------------------------------------------------------------------
# IOReport — per-component energy (CPU, GPU, ANE, DRAM) and GPU residency
# ---------------------------------------------------------------------------
class IOReportPower:
    def __init__(self):
        L = self.L = ctypes.cdll.LoadLibrary("/usr/lib/libIOReport.dylib")
        L.IOReportCopyChannelsInGroup.argtypes = [c_void_p, c_void_p, c_uint64, c_uint64, c_uint64]
        L.IOReportCopyChannelsInGroup.restype = c_void_p
        L.IOReportMergeChannels.argtypes = [c_void_p, c_void_p, c_void_p]
        L.IOReportMergeChannels.restype = None
        L.IOReportCreateSubscription.argtypes = [c_void_p, c_void_p, POINTER(c_void_p), c_uint64, c_void_p]
        L.IOReportCreateSubscription.restype = c_void_p
        L.IOReportCreateSamples.argtypes = [c_void_p, c_void_p, c_void_p]
        L.IOReportCreateSamples.restype = c_void_p
        L.IOReportCreateSamplesDelta.argtypes = [c_void_p, c_void_p, c_void_p]
        L.IOReportCreateSamplesDelta.restype = c_void_p
        for fn in ("IOReportChannelGetGroup", "IOReportChannelGetSubGroup", "IOReportChannelGetChannelName",
                   "IOReportChannelGetUnitLabel"):
            getattr(L, fn).argtypes = [c_void_p]
            getattr(L, fn).restype = c_void_p
        L.IOReportSimpleGetIntegerValue.argtypes = [c_void_p, c_int32]
        L.IOReportSimpleGetIntegerValue.restype = c_int64
        L.IOReportStateGetCount.argtypes = [c_void_p]
        L.IOReportStateGetCount.restype = c_int32
        L.IOReportStateGetNameForIndex.argtypes = [c_void_p, c_int32]
        L.IOReportStateGetNameForIndex.restype = c_void_p
        L.IOReportStateGetResidency.argtypes = [c_void_p, c_int32]
        L.IOReportStateGetResidency.restype = c_int64

        ch = L.IOReportCopyChannelsInGroup(cfstr("Energy Model"), None, 0, 0, 0)
        if not ch:
            raise OSError("no Energy Model channels")
        gpu = L.IOReportCopyChannelsInGroup(cfstr("GPU Stats"), cfstr("GPU Performance States"), 0, 0, 0)
        if gpu:
            L.IOReportMergeChannels(ch, gpu, None)
        self.desired = CF.CFDictionaryCreateMutableCopy(None, CF.CFDictionaryGetCount(ch), ch)
        self.subbed = c_void_p()
        self.sub = L.IOReportCreateSubscription(None, self.desired, byref(self.subbed), 0, None)
        if not self.sub:
            raise OSError("IOReport subscription failed")
        self.prev = L.IOReportCreateSamples(self.sub, self.subbed, None)
        self.prev_t = time.time()

    def read(self):
        L = self.L
        cur = L.IOReportCreateSamples(self.sub, self.subbed, None)
        t = time.time()
        dt, self.prev_t = t - self.prev_t, t
        if not cur:
            return None
        if dt <= 0 or dt > 15:  # the Mac slept (or we stalled): this delta isn't a rate, start over
            CF.CFRelease(self.prev)
            self.prev = cur
            return None
        delta = L.IOReportCreateSamplesDelta(self.prev, cur, None)
        CF.CFRelease(self.prev)
        self.prev = cur
        if not delta:
            return None
        chans = CF.CFDictionaryGetValue(delta, cfstr("IOReportChannels"))
        energy, gpu_active = {}, None
        for i in range(CF.CFArrayGetCount(chans) if chans else 0):
            c = CF.CFArrayGetValueAtIndex(chans, i)
            group = pystr(L.IOReportChannelGetGroup(c))
            name = pystr(L.IOReportChannelGetChannelName(c)) or ""
            if group == "Energy Model":
                unit = (pystr(L.IOReportChannelGetUnitLabel(c)) or "").strip()
                scale = {"mJ": 1e3, "uJ": 1e6, "nJ": 1e9}.get(unit)
                if scale:
                    energy[name] = L.IOReportSimpleGetIntegerValue(c, 0) / scale / dt
            elif group == "GPU Stats" and name == "GPUPH":
                total = idle = 0
                for s in range(L.IOReportStateGetCount(c)):
                    r = L.IOReportStateGetResidency(c, s)
                    total += r
                    if (pystr(L.IOReportStateGetNameForIndex(c, s)) or "").strip().upper() in ("OFF", "IDLE", "DOWN"):
                        idle += r
                if total:
                    gpu_active = 100.0 * (total - idle) / total
        CF.CFRelease(delta)

        def pick(pred):
            return round(sum(v for k, v in energy.items() if pred(k)), 2)
        if any(v > 400 for v in energy.values()):  # counter glitch; no Mac draws this much
            return None
        cpu = energy.get("CPU Energy")
        if cpu is None:
            cpu = pick(lambda k: k.endswith("CPU Energy") or re.match(r"^[EP]CPU\d* ?Energy", k or ""))
        gpu = energy.get("GPU Energy", energy.get("GPU"))
        if gpu is None:
            gpu = pick(lambda k: k.startswith("GPU") and "SRAM" not in k)
        return {"cpu": round(cpu or 0, 2), "gpu": round(gpu or 0, 2), "ane": pick(lambda k: k.startswith("ANE")),
                "dram": pick(lambda k: k.startswith("DRAM")), "gpu_active": gpu_active,
                "channels": {k: round(v, 3) for k, v in energy.items() if v > 0.001}}


# ---------------------------------------------------------------------------
# Per-core CPU load (Mach host_processor_info)
# ---------------------------------------------------------------------------
class CoreLoad:
    def __init__(self):
        LIBC.mach_host_self.restype = c_uint32
        LIBC.host_processor_info.argtypes = [c_uint32, c_int, POINTER(c_uint), POINTER(POINTER(c_uint)), POINTER(c_uint)]
        LIBC.host_processor_info.restype = c_int
        LIBC.vm_deallocate.argtypes = [c_uint32, c_size_t, c_size_t]
        self.host = LIBC.mach_host_self()
        self.task = c_uint32.in_dll(LIBC, "mach_task_self_").value
        self.prev = None
        p = int(sysctl("hw.perflevel0.logicalcpu") or 0)
        e = int(sysctl("hw.perflevel1.logicalcpu") or 0)
        self.kinds = (["E"] * e + ["P"] * p) if (p and e) else None

    def read(self):
        n, info, cnt = c_uint(), POINTER(c_uint)(), c_uint()
        if LIBC.host_processor_info(self.host, 2, byref(n), byref(info), byref(cnt)) != 0:
            return None
        ticks = [tuple(info[i * 4 + j] for j in range(4)) for i in range(n.value)]
        LIBC.vm_deallocate(self.task, ctypes.cast(info, c_void_p).value, cnt.value * 4)
        prev, self.prev = self.prev, ticks
        if not prev or len(prev) != len(ticks):
            return None
        out = []
        for i, (a, b) in enumerate(zip(prev, ticks)):
            d = [max(0, y - x) for x, y in zip(a, b)]  # user, system, idle, nice
            tot = sum(d) or 1
            out.append({"i": i, "k": self.kinds[i] if self.kinds and i < len(self.kinds) else "",
                        "u": round(100 * (d[0] + d[3]) / tot, 1), "s": round(100 * d[1] / tot, 1)})
        return out


class ThermalPressure:
    LEVELS = {0: "nominal", 1: "moderate", 2: "heavy", 3: "trapping", 4: "sleeping"}

    def __init__(self):
        LIBC.notify_register_check.argtypes = [c_char_p, POINTER(c_int)]
        LIBC.notify_get_state.argtypes = [c_int, POINTER(c_uint64)]
        self.token = c_int()
        if LIBC.notify_register_check(b"com.apple.system.thermalpressurelevel", byref(self.token)) != 0:
            raise OSError("no thermal notify")

    def read(self):
        st = c_uint64()
        LIBC.notify_get_state(self.token, byref(st))
        return self.LEVELS.get(st.value, str(st.value))


# ---------------------------------------------------------------------------
# Command-based collectors
# ---------------------------------------------------------------------------
def u64_signed(v):
    return v - (1 << 64) if isinstance(v, int) and v >= (1 << 63) else v


def battery():
    arr = run_plist(["ioreg", "-rn", "AppleSmartBattery", "-a"], timeout=4)
    if not arr:
        return None
    b = arr[0]
    volt = b.get("Voltage") or b.get("AppleRawBatteryVoltage") or 0
    amp = u64_signed(b.get("InstantAmperage", b.get("Amperage", 0))) or 0
    design = b.get("DesignCapacity") or 0
    raw_max = b.get("AppleRawMaxCapacity") or b.get("NominalChargeCapacity") or 0
    pct = b.get("CurrentCapacity")
    if b.get("MaxCapacity") and b.get("MaxCapacity") not in (100, 0):  # Intel Macs report mAh
        pct = round(100 * b["CurrentCapacity"] / b["MaxCapacity"])
    tele = b.get("PowerTelemetryData") or {}
    adapter = b.get("AdapterDetails") or b.get("AppleRawAdapterDetails") or {}
    if isinstance(adapter, list):
        adapter = adapter[0] if adapter else {}
    temp = b.get("Temperature") or b.get("VirtualTemperature")
    tr = b.get("TimeRemaining") or b.get("AvgTimeToEmpty")
    return {
        "percent": pct, "charging": bool(b.get("IsCharging")), "plugged": bool(b.get("ExternalConnected")),
        "full": bool(b.get("FullyCharged")), "cycles": b.get("CycleCount"),
        "health": round(100 * raw_max / design, 1) if design and raw_max else None,
        "design_mah": design, "max_mah": raw_max, "temp": round(temp / 100, 1) if temp else None,
        "voltage": round(volt / 1000, 2) if volt else None, "amps": round(amp / 1000, 2) if amp else 0,
        "watts": round(volt * amp / 1e6, 1) if volt and amp else 0,
        "time_min": tr if tr and tr < 65535 else None,
        "adapter_w": adapter.get("Watts"), "adapter_name": adapter.get("Name") or adapter.get("Description"),
        "system_load_w": round(tele["SystemLoad"] / 1000, 1) if tele.get("SystemLoad") else None,
        "power_in_w": round(tele["SystemPowerIn"] / 1000, 1) if tele.get("SystemPowerIn") else None,
        "condition": b.get("BatteryHealthCondition") or ("Normal" if b.get("PermanentFailureStatus") == 0 else None),
    }


def bt_batteries():
    arr = run_plist(["ioreg", "-r", "-l", "-k", "BatteryPercent", "-a"], timeout=4) or []
    out, seen = [], set()
    for d in arr:
        name = d.get("Product") or d.get("DeviceName") or d.get("Name")
        pct = d.get("BatteryPercent")
        if name and pct is not None and name not in seen:
            seen.add(name)
            out.append({"name": name, "percent": pct})
    return out


def parse_netstat(txt):
    """netstat -ibn -> {iface: (ibytes, obytes, ipkts, opkts)} using the <Link#> rows."""
    res = {}
    for line in txt.splitlines()[1:]:
        p = line.split()
        if len(p) < 10 or not p[2].startswith("<Link"):
            continue
        # columns: Name Mtu Network [Address] Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        nums = [x for x in p[3:] if x.isdigit() or x == "-"]
        if len(nums) < 7:
            continue
        try:
            ipk, ib, opk, ob = int(nums[-7]), int(nums[-5]), int(nums[-4]), int(nums[-2])
        except ValueError:
            continue
        res[p[0]] = (ib, ob, ipk, opk)
    return res


def disk_io():
    arr = run_plist(["ioreg", "-c", "IOBlockStorageDriver", "-r", "-w", "0", "-a"], timeout=4) or []
    r = w = ro = wo = 0
    for d in arr:
        st = d.get("Statistics") or {}
        r += st.get("Bytes (Read)", 0)
        w += st.get("Bytes (Write)", 0)
        ro += st.get("Operations (Read)", 0)
        wo += st.get("Operations (Write)", 0)
    return r, w, ro, wo, len(arr)


def parse_nettop(txt):
    """nettop -P -L 2 -d -x -J bytes_in,bytes_out -> {(name, pid): (in, out)} from the last sample.
    Tolerant of an optional leading time column."""
    lines = txt.splitlines()
    heads = [i for i, l in enumerate(lines) if "bytes_in" in l]
    if not heads:
        return {}
    hdr = lines[heads[-1]].split(",")
    try:
        ci, co = hdr.index("bytes_in"), hdr.index("bytes_out")
    except ValueError:
        return {}
    res = {}
    for line in lines[heads[-1] + 1:]:
        f = line.split(",")
        if len(f) <= max(ci, co):
            continue
        name = next((x for x in f[:ci] if re.match(r"^.+\.\d+$", x) and not re.match(r"^\d+:\d+", x)), None)
        if not name:
            continue
        m = re.match(r"^(.*)\.(\d+)$", name)
        try:
            res[(m.group(1), int(m.group(2)))] = (int(f[ci] or 0), int(f[co] or 0))
        except ValueError:
            pass
    return res


def ping(host):
    o = run(["ping", "-c", "1", "-t", "2", host], timeout=4)
    m = re.search(r"time[=<]([\d.]+) ms", o)
    return float(m.group(1)) if m else None


def gateway():
    m = re.search(r"gateway:\s*(\S+)", run(["route", "-n", "get", "default"], timeout=3))
    return m.group(1) if m else None


def interfaces():
    """Active interfaces with IPv4 addresses and a friendly name (Wi-Fi, Ethernet, Thunderbolt…)."""
    names = {}
    port = None
    for line in run(["networksetup", "-listallhardwareports"], timeout=4).splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port:
            names[line.split(":", 1)[1].strip()] = port
    out, cur = {}, None
    for line in run(["ifconfig"], timeout=3).splitlines():
        if line and not line[0].isspace():
            cur = line.split(":", 1)[0]
        elif cur and line.strip().startswith("inet ") and cur != "lo0":
            out.setdefault(cur, {"if": cur, "name": names.get(cur, "VPN" if cur.startswith("utun") else cur),
                                 "ip": line.split()[1]})
    return out


def wifi():
    d = run_plist(["system_profiler", "SPAirPortDataType", "-xml"], timeout=12)
    try:
        ifs = d[0]["_items"][0]["spairport_airport_interfaces"]
    except (TypeError, KeyError, IndexError):
        return None
    for i in ifs:
        cur = i.get("spairport_current_network_information")
        if cur:
            sn = cur.get("spairport_signal_noise", "")
            m = re.findall(r"(-?\d+)\s*dBm", sn)
            return {"ssid": cur.get("_name"), "channel": cur.get("spairport_network_channel"),
                    "phy": cur.get("spairport_network_phymode"), "rate": cur.get("spairport_network_rate"),
                    "security": (cur.get("spairport_security_mode") or "").replace("spairport_security_mode_", "").replace("_", " "),
                    "signal": int(m[0]) if m else None, "noise": int(m[1]) if len(m) > 1 else None,
                    "country": cur.get("spairport_network_country_code"), "if": i.get("_name")}
    return {"ssid": None, "off": True}


def volumes():
    out = []
    for line in run(["df", "-kl"], timeout=4).splitlines()[1:]:
        p = line.split(None, 8)
        if len(p) < 9:
            continue
        mount = p[8]
        if mount != "/" and not mount.startswith("/Volumes/"):
            continue
        try:
            total, used, avail = int(p[1]) * 1024, int(p[2]) * 1024, int(p[3]) * 1024
        except ValueError:
            continue
        out.append({"mount": mount, "name": "Macintosh HD" if mount == "/" else os.path.basename(mount),
                    "dev": p[0], "total": total, "used": total - avail, "free": avail})
    return out


def time_machine():
    o = run(["tmutil", "status"], timeout=5)
    if not o:
        return None
    g = lambda k: (re.search(r'\b%s = "?([^";]+)"?;' % k, o) or [None, None])[1]
    pct = g("Percent")
    return {"running": g("Running") == "1", "phase": g("BackupPhase"),
            "percent": round(float(pct) * 100) if pct and pct.replace(".", "", 1).replace("-", "").isdigit() else None}


def sleep_blockers():
    o = run(["pmset", "-g", "assertions"], timeout=4)
    out = []
    for line in o.splitlines():
        m = re.match(r'\s*pid (\d+)\(([^)]+)\):\s*\[[^\]]+\]\s*([\d:]+)\s+(\w+)\s+named:\s*"(.*)"', line)
        if m and m.group(4) in ("PreventUserIdleSystemSleep", "PreventUserIdleDisplaySleep", "PreventSystemSleep",
                                "NoIdleSleepAssertion", "NoDisplaySleepAssertion"):
            out.append({"pid": int(m.group(1)), "proc": m.group(2), "age": m.group(3), "type": m.group(4),
                        "reason": m.group(5)[:120]})
    return out


def usb_devices():
    out = []
    for line in run(["ioreg", "-p", "IOUSB", "-w", "0"], timeout=4).splitlines():
        m = re.search(r"\+-o (.+?)@[0-9a-f]+\s+<class (\w+)", line)
        if m and "Root" not in m.group(1) and m.group(2) != "AppleUSBXHCI":
            out.append(m.group(1).strip())
    return [d for d in out if not re.search(r"hub|xhci|root", d, re.I)][:30]


def displays():
    d = run_plist(["system_profiler", "SPDisplaysDataType", "-xml"], timeout=12)
    try:
        items = d[0]["_items"]
    except (TypeError, KeyError, IndexError):
        return None, []
    gpu, outs = None, []
    for it in items:
        gpu = gpu or {"name": it.get("sppci_model") or it.get("_name"), "cores": it.get("sppci_cores")}
        for dd in it.get("spdisplays_ndrvs", []):
            outs.append({"name": dd.get("_name"), "res": dd.get("_spdisplays_resolution") or dd.get("spdisplays_resolution"),
                         "main": dd.get("spdisplays_main") == "spdisplays_yes",
                         "builtin": "internal" in str(dd.get("spdisplays_connection_type", "")).lower()})
    return gpu, outs


def system_info():
    sw = run(["sw_vers"], timeout=3)
    g = lambda k: (re.search(r"%s:\s*(.+)" % k, sw) or [None, None])[1]
    boot = re.search(r"sec = (\d+)", sysctl("kern.boottime"))
    smart = re.search(r"SMART Status:\s*(.+)", run(["diskutil", "info", "disk0"], timeout=5))
    return {"model": sysctl("hw.model"), "chip": sysctl("machdep.cpu.brand_string"),
            "os": "macOS %s (%s)" % (g("ProductVersion") or "?", g("BuildVersion") or "?"),
            "cores_p": int(sysctl("hw.perflevel0.logicalcpu") or 0), "cores_e": int(sysctl("hw.perflevel1.logicalcpu") or 0),
            "cores": os.cpu_count(), "mem": int(sysctl("hw.memsize") or 0),
            "boot": int(boot.group(1)) if boot else None, "smart": smart.group(1).strip() if smart else None,
            "host": os.uname().nodename.split(".")[0], "kernel": os.uname().release}


# ---------------------------------------------------------------------------
# Aggregator used by the server
# ---------------------------------------------------------------------------
class MacStats:
    def __init__(self):
        self.errors = {}
        self.hid = self._try("temps", HIDTemps)
        self.smc = self._try("smc", SMC)
        self.ior = self._try("ioreport", IOReportPower)
        self.cores = self._try("cores", CoreLoad)
        self.therm = self._try("thermal", ThermalPressure)
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=150))
        self.prev_net = self.prev_io = self.prev_vm = None
        self.prev_t = None
        self.slow_data = {}
        self.static = self._safe(system_info) or {}
        gpu, disp = self._safe(displays) or (None, [])
        self.static["gpu"], self.static["displays"] = gpu, disp

    def _try(self, name, cls):
        if not MAC or CF is None:
            return None
        try:
            return cls()
        except Exception as e:
            self.errors[name] = str(e)
            return None

    @staticmethod
    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    def _push(self, k, v):
        if v is not None:
            self.hist[k].append(round(v, 2))
        return list(self.hist[k])

    def fast(self, vm_counters=None):
        """Cheap readings, every sample (~2 s)."""
        now = time.time()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        if dt is not None and (dt <= 0 or dt > 15):
            dt = None  # woke from sleep: skip one round of rate maths instead of reporting a giant spike
        out = {}
        # temperatures
        temps = self._safe(self.hid.read) if self.hid else None
        if temps:
            groups = collections.defaultdict(list)
            for k, v in temps.items():
                groups[classify_sensor(k)].append(v)
            cpu = groups.get("CPU / SoC") or groups.get("SoC (other)") or []
            out["temps"] = {"sensors": [{"name": k, "c": v, "group": classify_sensor(k)} for k, v in sorted(temps.items())],
                            "cpu_max": max(cpu) if cpu else None, "cpu_avg": round(sum(cpu) / len(cpu), 1) if cpu else None,
                            "gpu": max(groups["GPU"]) if groups.get("GPU") else None,
                            "ssd": max(groups["SSD"]) if groups.get("SSD") else None,
                            "battery": max(groups["Battery"]) if groups.get("Battery") else None}
            out["temps"]["hist"] = self._push("temp", out["temps"]["cpu_max"])
        out["thermal"] = self._safe(self.therm.read) if self.therm else None
        if self.smc:
            out["fans"] = self._safe(self.smc.fans) or []
            out["smc_power"] = self._safe(self.smc.power) or {}
        pw = self._safe(self.ior.read) if self.ior else None
        if pw:
            out["power"] = pw
        if self.cores:
            out["cores"] = self._safe(self.cores.read)
        bat = self._safe(battery)
        out["battery"] = bat
        # one "system power" number, best source first
        sysw = (out.get("smc_power") or {}).get("system") or (bat or {}).get("system_load_w")
        if not sysw and pw:
            sysw = pw["cpu"] + pw["gpu"] + pw["ane"] + pw["dram"]
        out["sys_power"] = round(sysw, 1) if sysw else None
        out["power_hist"] = self._push("power", out["sys_power"])
        if pw:
            self._push("cpu_w", pw["cpu"])
            self._push("gpu_w", pw["gpu"])

        # network throughput
        net = parse_netstat(run(["netstat", "-ibn"], timeout=3))
        rates = {}
        if self.prev_net and dt:
            for k, v in net.items():
                p = self.prev_net.get(k)
                if p and v[0] >= p[0] and v[1] >= p[1]:
                    down_r, up_r = (v[0] - p[0]) / dt, (v[1] - p[1]) / dt
                    if down_r < 12e9 and up_r < 12e9:
                        rates[k] = {"down": down_r, "up": up_r, "total_down": v[0], "total_up": v[1]}
        self.prev_net = net
        real = {k: v for k, v in rates.items() if k.startswith(("en", "utun", "bridge", "ppp")) or k.startswith("ipsec")}
        down = sum(v["down"] for k, v in real.items() if k.startswith("en"))
        up = sum(v["up"] for k, v in real.items() if k.startswith("en"))
        out["net"] = {"down": down, "up": up, "ifaces": real, "hist_down": self._push("down", down),
                      "hist_up": self._push("up", up)}

        # disk I/O
        io = self._safe(disk_io)
        if io:
            # a disk (or disk image) appearing/disappearing changes the totals by its whole lifetime: skip that round
            if self.prev_io and dt and self.prev_io[4] == io[4]:
                r, w = (io[0] - self.prev_io[0]) / dt, (io[1] - self.prev_io[1]) / dt
                ro, wo = (io[2] - self.prev_io[2]) / dt, (io[3] - self.prev_io[3]) / dt
                if 0 <= r < 20e9 and 0 <= w < 20e9:
                    out["io"] = {"read": r, "write": w, "riops": max(0, ro), "wiops": max(0, wo), "total_read": io[0],
                                 "total_write": io[1], "hist_r": self._push("io_r", r), "hist_w": self._push("io_w", w)}
            self.prev_io = io

        # memory paging rates
        if vm_counters:
            if self.prev_vm and dt:
                out["paging"] = {k: max(0, (vm_counters.get(k, 0) - self.prev_vm.get(k, 0)) / dt) for k in vm_counters}
            self.prev_vm = vm_counters
        return out

    def slow(self, which):
        """Heavier readings; `which` picks a subset so they can run at different rates."""
        d = self.slow_data
        if "net" in which:
            gw = self._safe(gateway)
            d["gateway"] = gw
            d["ping_internet"] = self._safe(ping, "1.1.1.1")
            d["ping_gateway"] = self._safe(ping, gw) if gw else None
            d["ifaces"] = self._safe(interfaces) or {}
            self._push("ping", d["ping_internet"])
            d["ping_hist"] = list(self.hist["ping"])
            nt = run(["nettop", "-P", "-L", "2", "-d", "-s", "1", "-x", "-J", "bytes_in,bytes_out"], timeout=6)
            d["nettop"] = [{"name": k[0], "pid": k[1], "down": v[0], "up": v[1]}
                           for k, v in sorted(parse_nettop(nt).items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:15]
                           if v[0] + v[1] > 0]
        if "misc" in which:
            d["bt"] = self._safe(bt_batteries) or []
            d["volumes"] = self._safe(volumes) or []
            d["tm"] = self._safe(time_machine)
            d["sleep_blockers"] = self._safe(sleep_blockers) or []
            d["usb"] = self._safe(usb_devices) or []
        if "wifi" in which:
            d["wifi"] = self._safe(wifi)
        return d


if __name__ == "__main__":
    if not MAC:
        sys.exit("macstats only works on macOS")
    s = MacStats()
    print("sources that failed:", s.errors or "none")
    s.fast()
    time.sleep(1.5)
    f = s.fast()
    sl = s.slow({"net", "misc", "wifi"})
    t = f.get("temps") or {}
    print("\nTemperatures: CPU max %s °C, avg %s °C, thermal pressure: %s" % (t.get("cpu_max"), t.get("cpu_avg"), f.get("thermal")))
    for x in t.get("sensors", []):
        print("   %-28s %6.1f °C   %s" % (x["name"], x["c"], x["group"]))
    print("\nFans:", f.get("fans"))
    print("SMC power:", f.get("smc_power"))
    print("IOReport power:", json.dumps(f.get("power"), indent=1))
    print("System power: %s W" % f.get("sys_power"))
    print("\nCores:", f.get("cores"))
    print("\nBattery:", json.dumps(f.get("battery"), indent=1))
    print("\nNetwork:", {k: v for k, v in f.get("net", {}).items() if not k.startswith("hist")})
    print("Disk I/O:", {k: v for k, v in (f.get("io") or {}).items() if not k.startswith("hist")})
    print("\nSlow:", json.dumps(sl, indent=1, default=str))
    print("\nStatic:", json.dumps(s.static, indent=1, default=str))
