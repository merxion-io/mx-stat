# -*- coding: utf-8 -*-
"""Merxion Stat · платформенный слой Linux.

Данные берутся из psutil, /proc, /sys/class/*, /etc и утилит ip / nmcli /
podman. Интерфейс модуля один в один повторяет platform_macos.py — список
функций и их смысл описан в platforms.py."""
from __future__ import annotations

import platform
import re
import time
from collections import Counter
from pathlib import Path

import psutil

from platform_common import ps_rows, sh, which

NAME = "linux"
# префиксы имён интерфейсов, которые считаем VPN-туннелями
VPN_PREFIXES = ("wg", "tun", "tap", "tailscale", "ppp")
LOOPBACK = "lo"
PORTS_SOURCE = "psutil"
TEMP_NOTE = "дисков без smartctl не видно"
POWER_NOTE = ("измерены CPU и видеокарты; диски, память, плата и БП — нет",
              "не измерены: диски, ОЗУ, плата", "без дисков и ОЗУ")
POWER_NOTE_DENIED = ("измерены видеокарты; диски, память, плата и БП — нет",
                     "не измерены: диски, ОЗУ, плата", "без дисков и ОЗУ")


# ═══════════════════════════ машина и система ══════════════════════════════
def host_name() -> str:
    return platform.node()


def cpu_brand() -> str:
    """Модель процессора из /proc/cpuinfo."""
    try:
        m = re.search(r"^model name\s*:\s*(.+)$",
                      Path("/proc/cpuinfo").read_text(), re.M)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def os_label() -> str:
    """Короткое имя дистрибутива для шапки."""
    try:
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?',
                      Path("/etc/os-release").read_text(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def kernel_short() -> str:
    return f"Linux {platform.release()}"


def machine_model() -> str:
    return platform.machine()


# ═══════════════════════════ процессор и память ════════════════════════════
def cpu_groups(n: int) -> list[tuple[str, str, list[int]]]:
    """[(название группы, префикс ядра, индексы)] — на x86 ядра равноправны."""
    return [("ядра", "C", list(range(n)))]


def cpu_summary(short: bool = False) -> str:
    phys = psutil.cpu_count(logical=False) or "?"
    if short:
        return f"{phys} физ."
    return f"{phys} ядер / {psutil.cpu_count()} потоков"


def memory_extra(m) -> list[tuple[str, int]]:
    """Дополнительные строки памяти: страничный кэш ядра."""
    return [("кэш", m.cached + m.buffers)]


# ═══════════════════════════ диски и батарея ═══════════════════════════════
def disk_name(part) -> str | None:
    """Имя раздела для показа или None — только реальные блочные устройства."""
    if not part.device.startswith("/dev/"):
        return None
    return "система" if part.mountpoint == "/" else Path(part.mountpoint).name


def battery_facts() -> list[tuple[str, str]]:
    """Циклы и здоровье батареи из /sys/class/power_supply (если ядро отдаёт)."""
    base = Path("/sys/class/power_supply")
    bats = sorted(base.glob("BAT*")) if base.is_dir() else []
    if not bats:
        return []
    b = bats[0]

    def num(fn: str) -> int:
        try:
            return int((b / fn).read_text().strip())
        except (OSError, ValueError):
            return 0
    out = []
    cyc = num("cycle_count")
    if cyc:
        out.append(("циклов", str(cyc)))
    full, design = (num("energy_full") or num("charge_full"),
                    num("energy_full_design") or num("charge_full_design"))
    if full and design:
        out.append(("здоровье", f"{full * 100 / design:.0f}%"))
    return out


# ═══════════════════════════════ сеть ══════════════════════════════════════
def default_iface() -> tuple[str, str]:
    """(интерфейс, шлюз) маршрута по умолчанию — из `ip route`."""
    rt = sh(["ip", "-4", "route", "show", "default"])
    m = re.search(r"\bdev\s+(\S+)", rt)
    g = re.search(r"\bvia\s+(\S+)", rt)
    return (m.group(1) if m else ""), (g.group(1) if g else "")


def net_counters() -> dict[str, tuple[int, int]]:
    """{iface: (rx_bytes, tx_bytes)} — счётчики интерфейсов с момента загрузки."""
    return {n: (c.bytes_recv, c.bytes_sent)
            for n, c in psutil.net_io_counters(pernic=True).items()}


def dns_servers() -> list[str]:
    out = []
    try:
        txt = Path("/etc/resolv.conf").read_text()
    except OSError:
        txt = ""
    for m in re.finditer(r"^nameserver\s+(\S+)", txt, re.M):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out[:3]


def wifi_ssid(iface: str) -> str:
    """Имя Wi-Fi сети, если интерфейс беспроводной."""
    if not iface or not Path(f"/sys/class/net/{iface}/wireless").exists():
        return ""
    for ln in sh(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
        if ln.startswith("yes:"):
            return ln.split(":", 1)[1]
    return ""


def _tcp_connections() -> list:
    """Все TCP-сокеты машины ([] если ядро не дало прочитать)."""
    try:
        return psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return []


def listening_sockets() -> list[tuple[int, str, int]]:
    """[(порт, адрес, pid)] слушающих TCP-сокетов.

    Сокеты читаются из /proc через psutil. Без root ядро отдаёт pid только
    для своих процессов — у чужих слушателей pid будет 0."""
    return [(c.laddr.port, c.laddr.ip, c.pid or 0) for c in _tcp_connections()
            if c.status == psutil.CONN_LISTEN and c.laddr]


def tcp_summary() -> dict[str, int]:
    """Счётчики TCP-соединений по состояниям (ESTABLISHED, TIME_WAIT, …)."""
    return dict(Counter(c.status for c in _tcp_connections()))


# ═══════════════════════════ контейнеры ════════════════════════════════════
def containers() -> dict | None:
    """{engine, state, rows} движка контейнеров или None, если движка нет."""
    for cli in ("podman", "docker"):
        if which(cli):
            return {"engine": cli, "state": "", "rows": ps_rows(cli)}
    return None


# ═══════════════════════ температуры и энергия ═════════════════════════════
POWERCAP = Path("/sys/class/powercap")
HWMON = Path("/sys/class/hwmon")
# как называть источники по-человечески
POWER_NAMES = {"package-0": "CPU", "package-1": "CPU 2",
               "core": "└ ядра", "uncore": "└ графика", "dram": "└ память"}
# как называть датчики температуры по-человечески
TEMP_CHIPS = {"k10temp": "CPU", "coretemp": "CPU", "zenpower": "CPU",
              "amdgpu": "iGPU", "nvme": "SSD", "acpitz": "плата"}
TEMP_LABELS = {"Tctl": "", "Tdie": "", "Package id 0": "", "edge": "",
               "Composite": "", "PHY Temperature": "PHY", "MAC Temperature": "MAC"}


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def temperatures() -> tuple[list, list]:
    """([процессор], [прочие узлы]) — строки (узел, °C, критическая °C).

    Основа — hwmon через psutil (CPU, встроенная графика, сетевой чип).
    Видеокарты NVIDIA core добавляет между этими списками сам."""
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        sensors = {}
    cpu, other = [], []
    for chip, entries in sensors.items():
        base = TEMP_CHIPS.get(chip)
        if base is None:                       # сетевые чипы и прочее — по имени
            base = "сеть" if chip.startswith(("en", "eth", "wl")) else _clip(chip, 9)
        for e in entries:
            label = TEMP_LABELS.get(e.label or "", e.label or "")
            if label.startswith("Tccd"):       # кристаллы (чиплеты) Ryzen
                name = f"└ чип {label[4:]}"
            else:
                name = f"{base} {label}".strip()
            row = (name, e.current, e.critical or e.high)
            (cpu if base == "CPU" else other).append(row)
    return cpu, other


def extra_power() -> list[tuple[str, float, bool]]:
    """[(имя, ватты, считать ли в сумму)] из hwmon: встроенная графика, датчики платы."""
    out = []
    if not HWMON.is_dir():
        return out
    for h in sorted(HWMON.glob("hwmon*")):
        try:
            chip = (h / "name").read_text().strip()
        except OSError:
            continue
        for f in sorted(h.glob("power*_input")):
            try:
                uw = int(f.read_text().strip())
            except (OSError, ValueError):
                continue
            label = ""
            lf = f.with_name(f.name.replace("_input", "_label"))
            try:
                label = lf.read_text().strip()
            except OSError:
                pass
            name = "iGPU" if chip == "amdgpu" else chip
            if label and chip != "amdgpu":
                name = f"{chip} {label}"
            out.append((name, uw / 1e6, True))
    return out


class PowerMeter:
    """Живой замер мощности CPU по счётчикам энергии RAPL.

    Ватты — прирост джоулей из `/sys/class/powercap` за время между замерами.
    Ядро отдаёт эти файлы только root, поэтому при отказе в доступе CPU честно
    помечается как «нет доступа»."""

    denied_note = ("CPU: нет доступа к счётчикам RAPL (файлы energy_uj читает только root)",
                   "CPU — нет доступа к RAPL")

    def __init__(self) -> None:
        self.prev: dict[str, tuple[int, float]] = {}
        self.watts: dict[str, float] = {}
        self.denied = False
        self.sample()

    def _domains(self) -> list[tuple[str, Path]]:
        out = []
        if not POWERCAP.is_dir():
            return out
        for d in sorted(POWERCAP.glob("intel-rapl:*")):
            try:
                out.append(((d / "name").read_text().strip(), d))
            except OSError:
                continue
        return out

    def sample(self) -> None:
        """Снять счётчики; ватты появятся со второго вызова."""
        now = time.time()
        seen = False
        for name, d in self._domains():
            try:
                uj = int((d / "energy_uj").read_text())
            except PermissionError:
                self.denied = True
                continue
            except (OSError, ValueError):
                continue
            seen = True
            prev = self.prev.get(name)
            if prev:
                duj, dt = uj - prev[0], now - prev[1]
                if duj < 0:      # счётчик переполнился
                    try:
                        duj += int((d / "max_energy_range_uj").read_text())
                    except (OSError, ValueError):
                        duj = 0
                if dt > 0.05 and duj >= 0:
                    self.watts[name] = duj / 1e6 / dt
            self.prev[name] = (uj, now)
        if seen:
            self.denied = False

    def cpu_rows(self) -> list[tuple[str, float, bool]]:
        """[(подпись, ватты, считать ли в сумму)]: пакет целиком, затем его части."""
        rows = []
        for name in sorted(self.watts, key=lambda n: (not n.startswith("package"), n)):
            pkg = name.startswith("package")
            rows.append((POWER_NAMES.get(name, name), self.watts[name], pkg))
        return rows
