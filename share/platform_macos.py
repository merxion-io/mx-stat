# -*- coding: utf-8 -*-
"""Merxion Stat · платформенный слой macOS.

Данные берутся из psutil и штатных утилит: sysctl, ioreg, vm_stat, netstat,
lsof, route, scutil, networksetup. Root не нужен. Интерфейс модуля один в один
повторяет platform_linux.py — список функций и их смысл описан в platforms.py."""
from __future__ import annotations

import ctypes as C
import ctypes.util
import platform
import plistlib
import re
import struct
import threading
import time
from collections import Counter
from pathlib import Path

import psutil

from platform_common import NoPower, ps_rows, sh, which

NAME = "macos"
# utun — системные туннели macOS (WireGuard, Tailscale, корпоративные VPN)
VPN_PREFIXES = ("utun", "ppp", "ipsec")
LOOPBACK = "lo0"
PORTS_SOURCE = "lsof"
TEMP_NOTE = "датчики CPU без root не видны — показана батарея"
POWER_NOTE = ("вся машина целиком по данным контроллера питания; CPU и GPU "
              "отдельно без root не видны",
              "вся машина целиком, без root", "вся машина")
POWER_NOTE_DENIED = POWER_NOTE


def sysctl(name: str) -> str:
    return sh(["sysctl", "-n", name])


# ═══════════════════════════ машина и система ══════════════════════════════
def host_name() -> str:
    return sh(["scutil", "--get", "ComputerName"]) or platform.node()


def cpu_brand() -> str:
    return sysctl("machdep.cpu.brand_string") or platform.processor() or platform.machine()


def os_label() -> str:
    return f"macOS {platform.mac_ver()[0]}"


def kernel_short() -> str:
    kv_ = sysctl("kern.version")
    m = re.search(r"Darwin Kernel Version ([\d.]+).*?(xnu-[\d.]+)", kv_)
    return f"Darwin {m.group(1)} · {m.group(2)}" if m else f"Darwin {platform.release()}"


def machine_model() -> str:
    return sysctl("hw.model") or platform.machine()


# ═══════════════════════════ процессор и память ════════════════════════════
_CLUSTERS: dict[int, str] | None = None


def _cpu_clusters() -> dict[int, str]:
    """{логический номер CPU: 'P' | 'E'} из дерева устройств (один раз).

    На Apple Silicon первыми идут E-ядра (на M1 cpu0-3 — E, cpu4-7 — P),
    поэтому «первые N — производительные» неверно; берём cluster-type."""
    global _CLUSTERS
    if _CLUSTERS is not None:
        return _CLUSTERS
    out: dict[int, str] = {}
    cur: dict[str, str] = {}
    txt = sh(["ioreg", "-p", "IODeviceTree", "-c", "IOPlatformDevice",
              "-k", "cluster-type", "-r", "-w0"], timeout=4)
    for ln in txt.splitlines():
        if "+-o " in ln:
            cur = {}
            continue
        m = re.search(r'"(cluster-type|logical-cpu-id)" = <?"?(\w+)"?>?', ln)
        if m:
            cur[m.group(1)] = m.group(2)
            if "cluster-type" in cur and "logical-cpu-id" in cur:
                try:
                    out[int(cur["logical-cpu-id"])] = cur["cluster-type"]
                except ValueError:
                    pass
    if not out:
        # запасной путь: E-кластер идёт первым, его размер — perflevel1
        e = int(sysctl("hw.perflevel1.logicalcpu") or 0)
        p = int(sysctl("hw.perflevel0.logicalcpu") or 0)
        if e and p:
            out = {i: ("E" if i < e else "P") for i in range(e + p)}
    _CLUSTERS = out
    return out


def cpu_groups(n: int) -> list[tuple[str, str, list[int]]]:
    """[(название группы, префикс ядра, индексы)]: P- и E-кластеры Apple Silicon."""
    cl = _cpu_clusters()
    p_idx = [i for i in range(n) if cl.get(i) == "P"]
    e_idx = [i for i in range(n) if cl.get(i) == "E"]
    if not p_idx or not e_idx:                 # Intel-мак или неизвестная раскладка
        return [("ядра", "C", list(range(n)))]
    return [("P-кластер", "P", p_idx), ("E-кластер", "E", e_idx)]


def cpu_summary(short: bool = False) -> str:
    p = int(sysctl("hw.perflevel0.logicalcpu") or 0)
    e = int(sysctl("hw.perflevel1.logicalcpu") or 0)
    if p and e:
        return f"{p}P + {e}E" + ("" if short else " ядер")
    phys = psutil.cpu_count(logical=False) or "?"
    if short:
        return f"{phys} физ."
    return f"{phys} ядер / {psutil.cpu_count()} потоков"


_VM = {"at": 0.0, "txt": ""}


def _vm_stat() -> str:
    """Вывод vm_stat, кэш на 2 с: карточки памяти спрашивают его каждый тик."""
    now = time.time()
    if now - _VM["at"] > 2:
        _VM["txt"], _VM["at"] = sh(["vm_stat"]), now
    return _VM["txt"]


def memory_extra(m) -> list[tuple[str, int]]:
    """Дополнительные строки памяти: wired и сжатое компрессором."""
    comp = 0
    vm = _vm_stat()
    pg = re.search(r"page size of (\d+)", vm)
    oc = re.search(r"occupied by compressor:\s+(\d+)", vm)
    if pg and oc:
        comp = int(oc.group(1)) * int(pg.group(1))
    return [("wired", m.wired), ("сжато", comp)]


# ═══════════════════════════ диски и батарея ═══════════════════════════════
def disk_name(part) -> str | None:
    """Имя тома для показа или None.

    Системный том смонтирован дважды (ro-снапшот «/» и rw «Data») — брать
    нужно Data, иначе «занято» показывает 16 GB вместо реальных сотен."""
    mp = part.mountpoint
    if mp == "/System/Volumes/Data":
        return "система"
    if mp.startswith("/Volumes/"):
        return Path(mp).name
    return None


_BATT = {"at": 0.0, "txt": ""}
_BATT_LOCK = threading.Lock()


def _battery_io() -> str:
    """Вывод ioreg AppleSmartBattery, кэш на 5 с (его спрашивают каждый тик)."""
    with _BATT_LOCK:
        now = time.time()
        if now - _BATT["at"] > 5:
            _BATT["txt"] = sh(["ioreg", "-rn", "AppleSmartBattery"], timeout=4)
            _BATT["at"] = now
        return _BATT["txt"]


def _field(io: str, name: str, default: int = 0) -> int:
    m = re.search(rf'^\s*"{name}" = (-?\d+)', io, re.M)
    return int(m.group(1)) if m else default


def battery_facts() -> list[tuple[str, str]]:
    """Циклы, здоровье и температура батареи из ioreg AppleSmartBattery."""
    io = _battery_io()
    if not io:
        return []
    design = _field(io, "DesignCapacity", 0)
    raw = _field(io, "AppleRawMaxCapacity")
    out = [("циклов", str(_field(io, "CycleCount")))]
    if design:
        out.append(("здоровье", f"{raw * 100 / design:.0f}%"))
    t = _field(io, "Temperature")
    if t:
        out.append(("t° батареи", f"{t / 100:.1f} °C"))
    return out


# ── подробная батарея для вкладки «Батарея»: plist из ioreg -a, раз в секунду ──
NO_TIME = 65535                  # «времени нет» у контроллера (AvgTimeToFull и др.)


def _battery_plist() -> dict | None:
    """Словарь AppleSmartBattery из `ioreg -rn AppleSmartBattery -a` (~30 мс, без кэша)."""
    out = sh(["ioreg", "-rn", "AppleSmartBattery", "-a"], timeout=4)
    if not out:
        return None
    try:
        data = plistlib.loads(out.encode())
    except Exception:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _signed(v) -> int | None:
    """Знаковое из беззнакового 64-битного: 2^64−527 → −527. Не число → None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v - (1 << 64) if v >= 1 << 63 else v


def _num(d: dict | None, key: str) -> int | None:
    return _signed(d.get(key)) if isinstance(d, dict) else None


def _minutes(v: int | None) -> int | None:
    return v if v is not None and 0 < v < NO_TIME else None


def _adapter(io: dict) -> dict | None:
    """Блок питания: AdapterDetails (или первый из AppleRawAdapterDetails).

    Без блока macOS оставляет {'FamilyCode': 0} — это «нет адаптера»."""
    ad = io.get("AdapterDetails")
    if not isinstance(ad, dict) or not (ad.get("Watts") or ad.get("Name") or ad.get("AdapterVoltage")):
        raw = io.get("AppleRawAdapterDetails")
        ad = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], dict) else None
    if not ad or not (ad.get("Watts") or ad.get("Name") or ad.get("AdapterVoltage")):
        return None
    profiles = []
    for p in ad.get("UsbHvcMenu") or []:
        if isinstance(p, dict) and p.get("MaxVoltage"):
            profiles.append((p["MaxVoltage"] / 1000, (p.get("MaxCurrent") or 0) / 1000))
    hvc = _num(ad, "UsbHvcHvcIndex")
    volt, cur = _num(ad, "AdapterVoltage"), _num(ad, "Current")
    return {
        "watts": _num(ad, "Watts"),
        "voltage": volt / 1000 if volt else None,
        "current": cur / 1000 if cur else None,
        "name": str(ad.get("Name") or ad.get("Description") or ""),
        "maker": str(ad.get("Manufacturer") or ""),
        "wireless": bool(ad.get("IsWireless")),
        "family": _num(ad, "FamilyCode"),
        "profiles": profiles,
        "active": hvc if hvc is not None and 0 <= hvc < len(profiles) else None,
    }


def parse_battery(io: dict | None) -> dict | None:
    """Свести словарь AppleSmartBattery к общему виду battery_detail().

    Набор ключей зависит от модели (Intel-контроллер bq20z451, Apple Silicon
    и т. д.), поэтому всё, чего нет, — None, а не ноль."""
    if not io or io.get("BatteryInstalled") is False:
        return None
    bd = io.get("BatteryData") if isinstance(io.get("BatteryData"), dict) else {}
    ch = io.get("ChargerData") if isinstance(io.get("ChargerData"), dict) else {}
    pt = io.get("PowerTelemetryData") if isinstance(io.get("PowerTelemetryData"), dict) else {}
    life = bd.get("LifetimeData") if isinstance(bd.get("LifetimeData"), dict) else {}

    volt = _num(io, "Voltage") or _num(io, "AppleRawBatteryVoltage") or _num(bd, "Voltage")
    amp = _num(io, "InstantAmperage")
    if amp is None:
        amp = _num(io, "Amperage")
    ext = bool(io.get("ExternalConnected"))
    charging = bool(io.get("IsCharging"))
    full = bool(io.get("FullyCharged"))
    if charging:
        status = "charging"
    elif full and ext:
        status = "full"
    elif ext:
        status = "idle"                          # на блоке, но не заряжается
    else:
        status = "discharging"

    cells = [c / 1000 for c in (bd.get("CellVoltage") or []) if isinstance(c, int) and c > 0]
    ra = [r for r in (bd.get("WeightedRa") or []) if isinstance(r, int) and r > 0]
    qmax = [q for q in (bd.get("Qmax") or []) if isinstance(q, int) and q > 0]

    # поток мощности (мВт): от блока, потребление системы, батарея (+ заряд / − разряд)
    flow = None
    if pt:
        def w(key: str) -> float | None:
            v = _num(pt, key)
            return v / 1000 if v is not None else None
        sv, sc = _num(pt, "SystemVoltageIn"), _num(pt, "SystemCurrentIn")
        flow = {"in": w("SystemPowerIn"), "load": w("SystemLoad"), "batt": w("BatteryPower"),
                "loss": w("AdapterEfficiencyLoss"), "wall": w("WallEnergyEstimate"),
                "in_v": sv / 1000 if sv else None, "in_a": sc / 1000 if sc else None}
    elif isinstance(bd.get("SystemPower"), (int, float)):
        flow = {"in": None, "load": float(bd["SystemPower"]), "batt": None, "loss": None,
                "wall": None, "in_v": None, "in_a": None}

    temp = _num(io, "Temperature")
    t_life = {k: (_num(life, k) / 10 if _num(life, k) is not None else None)
              for k in ("MinimumTemperature", "MaximumTemperature", "AverageTemperature")}
    lv = {k: _num(life, k) for k in ("MinimumPackVoltage", "MaximumPackVoltage",
                                     "MaximumChargeCurrent", "MaximumDischargeCurrent")}
    lifetime = {
        "v_min": lv["MinimumPackVoltage"] / 1000 if lv["MinimumPackVoltage"] else None,
        "v_max": lv["MaximumPackVoltage"] / 1000 if lv["MaximumPackVoltage"] else None,
        "i_chg_max": lv["MaximumChargeCurrent"] / 1000 if lv["MaximumChargeCurrent"] else None,
        "i_dis_max": lv["MaximumDischargeCurrent"] / 1000 if lv["MaximumDischargeCurrent"] else None,
        "t_min": t_life["MinimumTemperature"], "t_max": t_life["MaximumTemperature"],
        "t_avg": t_life["AverageTemperature"],
        "hours": _num(life, "TotalOperatingTime"),
    } if life else {}

    cv, cc = _num(ch, "ChargingVoltage"), _num(ch, "ChargingCurrent")
    charger = {
        "voltage": cv / 1000 if cv else None,
        "current": cc / 1000 if cc is not None else None,
        "not_charging": _num(ch, "NotChargingReason"),
        "slow": _num(ch, "SlowChargingReason"),
        "vac_limit": (_num(ch, "VacVoltageLimit") or 0) / 1000 or None,
    } if ch else {}

    batt_w = flow["batt"] if flow and flow.get("batt") is not None else (
        volt * amp / 1e6 if volt and amp is not None else None)
    return {
        "source": "adapter" if ext else "battery",
        "status": status,
        "percent": _num(io, "CurrentCapacity"),
        "voltage": volt / 1000 if volt else None,
        "current": amp / 1000 if amp is not None else None,
        "power": batt_w,
        "time_empty": None if ext else _minutes(_num(io, "TimeRemaining") or _num(io, "AvgTimeToEmpty")),
        "time_full": _minutes(_num(io, "AvgTimeToFull")) if charging else None,
        "cells": cells,
        "cells_ra": ra,
        "qmax": qmax,
        "unit": "мА·ч",
        "cap_now": _num(io, "AppleRawCurrentCapacity"),
        "cap_full": _num(io, "AppleRawMaxCapacity"),
        "cap_nominal": _num(io, "NominalChargeCapacity"),
        "cap_design": _num(io, "DesignCapacity") or _num(bd, "DesignCapacity"),
        "cycles": _num(io, "CycleCount"),
        "cycles_design": _num(io, "DesignCycleCount9C"),
        "temp": temp / 100 if temp else None,
        "lifetime": lifetime,
        "charger": charger,
        "adapter": _adapter(io) if ext else None,
        "flow": flow,
        "device": str(io.get("DeviceName") or ""),
        "updated": _num(io, "UpdateTime"),       # когда контроллер сам обновил цифры (epoch)
        "src_note": "ioreg AppleSmartBattery",
    }


class _SMCVers(C.Structure):
    _fields_ = [("major", C.c_uint8), ("minor", C.c_uint8), ("build", C.c_uint8),
                ("reserved", C.c_uint8), ("release", C.c_uint16)]


class _SMCPLimit(C.Structure):
    _fields_ = [("version", C.c_uint16), ("length", C.c_uint16), ("cpu", C.c_uint32),
                ("gpu", C.c_uint32), ("mem", C.c_uint32)]


class _SMCInfo(C.Structure):
    _fields_ = [("size", C.c_uint32), ("type", C.c_uint32), ("attr", C.c_uint8)]


class _SMCData(C.Structure):
    """SMCKeyData_t из AppleSMC — та же раскладка, что у smcFanControl / Stats (80 байт)."""
    _fields_ = [("key", C.c_uint32), ("vers", _SMCVers), ("plimit", _SMCPLimit),
                ("info", _SMCInfo), ("result", C.c_uint8), ("status", C.c_uint8),
                ("cmd", C.c_uint8), ("data32", C.c_uint32), ("bytes", C.c_uint8 * 32)]


class _SMC:
    """Чтение ключей SMC через IOKit (AppleSMC) — без root, только чтение.

    Зачем: AppleSmartBattery в ioreg обновляется контроллером раз в ~60 с,
    а SMC отдаёт мощность системы, вход от блока, напряжение и ток батареи
    каждую секунду."""

    def __init__(self) -> None:
        self.conn = None
        self.lock = threading.Lock()
        self.failed = False

    def _open(self) -> bool:
        if self.conn is not None or self.failed:
            return self.conn is not None
        try:
            iokit = C.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
            libc = C.cdll.LoadLibrary(ctypes.util.find_library("c"))
            iokit.IOServiceMatching.restype = C.c_void_p
            iokit.IOServiceMatching.argtypes = [C.c_char_p]
            iokit.IOServiceGetMatchingService.restype = C.c_uint32
            iokit.IOServiceGetMatchingService.argtypes = [C.c_uint32, C.c_void_p]
            iokit.IOServiceOpen.argtypes = [C.c_uint32, C.c_uint32, C.c_uint32,
                                            C.POINTER(C.c_uint32)]
            iokit.IOConnectCallStructMethod.argtypes = [C.c_uint32, C.c_uint32, C.c_void_p,
                                                        C.c_size_t, C.c_void_p,
                                                        C.POINTER(C.c_size_t)]
            svc = iokit.IOServiceGetMatchingService(0, iokit.IOServiceMatching(b"AppleSMC"))
            conn = C.c_uint32()
            task = C.c_uint32.in_dll(libc, "mach_task_self_").value
            if not svc or iokit.IOServiceOpen(svc, task, 0, C.byref(conn)) != 0:
                raise OSError("AppleSMC не открылся")
            self.iokit, self.conn = iokit, conn.value
        except Exception:
            self.failed = True
        return self.conn is not None

    def _call(self, inp):
        out = _SMCData()
        size = C.c_size_t(C.sizeof(_SMCData))
        rc = self.iokit.IOConnectCallStructMethod(self.conn, 2, C.byref(inp), C.sizeof(_SMCData),
                                                  C.byref(out), C.byref(size))
        return out if rc == 0 and out.result == 0 else None

    def read(self, key: str):
        """(тип, сырые байты) ключа или None."""
        with self.lock:
            if not self._open():
                return None
            k = int.from_bytes(key.encode(), "big")
            q = _SMCData(key=k, cmd=9)                 # 9 — узнать размер и тип
            info = self._call(q)
            if info is None or not info.info.size:
                return None
            q = _SMCData(key=k, cmd=5)                 # 5 — прочитать значение
            q.info.size = info.info.size
            val = self._call(q)
            if val is None:
                return None
            typ = info.info.type.to_bytes(4, "big").decode("latin-1")
            return typ, bytes(val.bytes[:info.info.size])


_SMC_DEV = _SMC()


def _smc_float(key: str) -> float | None:
    r = _SMC_DEV.read(key)
    if r and r[0] == "flt " and len(r[1]) == 4:
        v = struct.unpack("<f", r[1])[0]
        return v if v == v and abs(v) < 1e4 else None     # NaN и мусор — мимо
    return None


def _smc_int(key: str, order: str) -> int | None:
    r = _SMC_DEV.read(key)
    if r and r[0] in ("si16", "ui16") and len(r[1]) == 2:
        return int.from_bytes(r[1], order, signed=r[0] == "si16")
    return None


def smc_live(ref: dict | None = None) -> dict | None:
    """Посекундные цифры SMC: {sys, in Вт, volt В, amp А} (чего нет — None).

    Порядок байт целых ключей у Apple Silicon и Intel разный, поэтому ток
    B0AC берём, только если B0AV в том же порядке совпал с напряжением
    батареи (SBAV или ioreg) — иначе цифре нельзя верить."""
    if _SMC_DEV.failed:
        return None
    sys_w, in_w, volt = _smc_float("PSTR"), _smc_float("PDTR"), _smc_float("SBAV")
    ref_v = volt or (ref or {}).get("voltage")
    amp = None
    if ref_v:
        for order in (("little", "big") if platform.machine() == "arm64" else ("big", "little")):
            mv = _smc_int("B0AV", order)
            if mv and abs(mv / 1000 - ref_v) < 0.5:
                ma = _smc_int("B0AC", order)
                if ma is not None and abs(ma) < 15000:
                    amp = ma / 1000
                    volt = volt or mv / 1000
                break
    if sys_w is None and in_w is None and volt is None and amp is None:
        return None
    return {"sys": sys_w, "in": in_w if in_w is None or in_w > 0.05 else 0.0,
            "volt": volt, "amp": amp}


def battery_detail() -> dict | None:
    """Подробная живая батарея (см. platforms.py); None — батареи нет."""
    d = parse_battery(_battery_plist())
    if d is not None:
        try:
            d["live"] = smc_live(d)
        except Exception:
            d["live"] = None
    return d


# ═══════════════════════════════ сеть ══════════════════════════════════════
def default_iface() -> tuple[str, str]:
    rt = sh(["route", "-n", "get", "default"])
    m = re.search(r"interface: (\S+)", rt)
    g = re.search(r"gateway: (\S+)", rt)
    return (m.group(1) if m else ""), (g.group(1) if g else "")


def net_counters() -> dict[str, tuple[int, int]]:
    """{iface: (rx_bytes, tx_bytes)} из netstat -ib.

    psutil на macOS отдаёт обрезанные 32-битные счётчики (176 MB вместо
    34 GB). Колонка Address есть не у всех интерфейсов (у lo0 её нет), поэтому
    позиции слева «плывут» — считаем с конца: … Ibytes Opkts Oerrs Obytes Coll."""
    out = {}
    for ln in sh(["netstat", "-ib"]).splitlines():
        f = ln.split()
        if len(f) >= 10 and f[2].startswith("<Link"):
            try:
                out[f[0]] = (int(f[-5]), int(f[-2]))
            except ValueError:
                continue
    return out


def dns_servers() -> list[str]:
    out = []
    for m in re.finditer(r"nameserver\[\d+\] : (\S+)", sh(["scutil", "--dns"])):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out[:3]


def wifi_ssid(iface: str) -> str:
    """Имя Wi-Fi сети (новые macOS прячут его без доступа к геопозиции)."""
    if not iface:
        return ""
    out = sh(["networksetup", "-getairportnetwork", iface])
    return out.split(": ", 1)[1] if "Current Wi-Fi Network:" in out else ""


def listening_sockets() -> list[tuple[int, str, int]]:
    """[(порт, адрес, pid)] слушающих TCP-сокетов из lsof.

    lsof режет имена процессов до 9 символов — имя core берёт через psutil."""
    out = []
    for ln in sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=4).splitlines()[1:]:
        f = ln.split()
        if len(f) < 9:
            continue
        host, _, port = f[8].rpartition(":")
        try:
            out.append((int(port), host, int(f[1])))
        except ValueError:
            continue
    return out


def tcp_summary() -> dict[str, int]:
    """Счётчики TCP-соединений по состояниям (ESTABLISHED, TIME_WAIT, …)."""
    c: Counter = Counter()
    for ln in sh(["netstat", "-an", "-p", "tcp"]).splitlines():
        f = ln.split()
        if len(f) >= 6 and f[0].startswith("tcp"):
            c[f[5]] += 1
    return dict(c)


# ═══════════════════════════ контейнеры ════════════════════════════════════
def containers() -> dict | None:
    """{engine, state, rows} или None.

    OrbStack сам поднимается от любого вызова docker, поэтому сперва
    спрашиваем `orb status` и зовём docker ps, только если он уже запущен."""
    if which("orb"):
        st = sh(["orb", "status"], timeout=6) or "неизвестно"
        if st.lower() != "running":
            ru = {"stopped": "остановлен"}.get(st.lower(), st)
            return {"engine": "OrbStack", "state": ru, "rows": []}
        return {"engine": "OrbStack", "state": "", "rows": ps_rows("docker")}
    for cli in ("podman", "docker"):
        if which(cli):
            return {"engine": cli, "state": "", "rows": ps_rows(cli)}
    return None


# ═══════════════════════ температуры и энергия ═════════════════════════════
def temperatures() -> tuple[list, list]:
    """([процессор], [прочие узлы]) — строки (узел, °C, критическая °C).

    Датчики SoC без root (powermetrics) недоступны, psutil на macOS
    температур не отдаёт; без root честно видна только батарея."""
    t = _field(_battery_io(), "Temperature")
    return [], ([("батарея", t / 100, 45.0)] if t else [])


def extra_power() -> list[tuple[str, float, bool]]:
    """[(имя, ватты, считать ли в сумму)]: вся машина от контроллера питания
    и, при работе от батареи, сколько из этого она отдаёт (в сумму не идёт)."""
    io = _battery_io()
    m = re.search(r'"SystemLoad"=(\d+)', io)
    out = [("машина", int(m.group(1)) / 1000, True)] if m else []
    if '"ExternalConnected" = No' in io:
        amp = _field(io, "InstantAmperage") or _field(io, "Amperage")
        if amp >= 1 << 63:                     # знаковое 64-битное в беззнаковом виде
            amp -= 1 << 64
        volt = _field(io, "Voltage")
        if amp < 0 and volt:
            out.append(("└ батарея", -amp * volt / 1e6, False))
    return out


class PowerMeter(NoPower):
    """CPU отдельно без root (powermetrics) не измерить — замерщик пустой."""
