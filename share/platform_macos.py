# -*- coding: utf-8 -*-
"""Merxion Stat · платформенный слой macOS.

Данные берутся из psutil и штатных утилит: sysctl, ioreg, vm_stat, netstat,
lsof, route, scutil, networksetup. Root не нужен. Интерфейс модуля один в один
повторяет platform_linux.py — список функций и их смысл описан в platforms.py."""
from __future__ import annotations

import platform
import re
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
