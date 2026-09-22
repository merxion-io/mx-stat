# -*- coding: utf-8 -*-
"""Merxion Stat · core — сбор системных данных и статичная отрисовка через rich.

Общая база для снапшота (mx stat -s) и полноэкранного TUI (mx stat).
Линуксовая версия: данные берутся из psutil, /proc, /etc и ip/podman."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import psutil
from rich import box
from rich.console import Console, Group
from rich.columns import Columns
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

HOME = Path.home()
CLAUDE = HOME / ".claude"
# префиксы имён интерфейсов, которые считаем VPN-туннелями
VPN_PREFIXES = ("wg", "tun", "tap", "tailscale", "ppp")

# ─────────────────────────── палитра tokyo-night ───────────────────────────
FG, DIM = "#c0caf5", "#565f89"
BLUE, CYAN, GREEN = "#7aa2f7", "#7dcfff", "#9ece6a"
YELLOW, RED, PURPLE = "#e0af68", "#f7768e", "#bb9af7"
WHITE = "#ffffff"

# ─────────────────────────── логотип MERXION ───────────────────────────────
BRAND = "MERXION"
LOGO = (
    "█▀▄▀█ █▀▀ █▀█ ▀▄▀ █ █▀█ █▄ █",
    "█ ▀ █ ██▄ █▀▄ ▄▀▄ █ █▄█ █ ▀█",
)
LOGO_W = len(LOGO[0])


def logo_lines(style: str = f"bold {WHITE}") -> list[Text]:
    """Двухстрочный блочный логотип MERXION (белый по умолчанию)."""
    return [Text(ln, style=style) for ln in LOGO]


def sh(cmd: list[str] | str, timeout: float = 3.0) -> str:
    """Тихо выполнить команду, вернуть stdout ('' при любой ошибке)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def hbytes(n: float) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}" if div >= 1 << 30 else f"{n / div:.0f} {unit}"
    return f"{n:.0f} B"


def htok(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def hnum(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def dur(seconds: float) -> str:
    s = int(max(0, seconds))
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м"
    return f"{s}с"


def ago(seconds: float) -> str:
    """Короткое «сколько назад»: 12с · 3м · 1ч05 · 2д."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м"
    if s < 86400:
        return f"{s // 3600}ч{(s % 3600) // 60:02d}"
    return f"{s // 86400}д"


def bar(pct: float, width: int = 18, lo: float = 65, hi: float = 88,
        color: str | None = None) -> Text:
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct * width / 100))
    if color is None:
        color = GREEN if pct < lo else (YELLOW if pct < hi else RED)
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style=DIM)
    return t


def barw(width: int) -> int:
    """Ширина полосы так, чтобы подпись справа гарантированно влезла в строку."""
    return min(40, max(12, width - 62))


def kv(label: str, value, note: str = "") -> Text:
    t = Text(" ")
    t.append(f"{label:<10}", style=DIM)
    t.append_text(value if isinstance(value, Text) else Text(str(value)))
    if note:
        t.append("  ")
        t.append(note, style=DIM)
    return t


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


# ═══════════════════════════ сбор данных ═══════════════════════════════════
TS_RE = re.compile(rb'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})')
TSF_RE = re.compile(rb'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
OUT_RE = re.compile(rb'"output_tokens":(\d+)')
IN_RE = re.compile(rb'"input_tokens":(\d+)')
CR_RE = re.compile(rb'"cache_read_input_tokens":(\d+)')
CC_RE = re.compile(rb'"cache_creation_input_tokens":(\d+)')
THINK_RE = re.compile(rb'"thinking_tokens":(\d+)')
SID_RE = re.compile(rb'"sessionId":"([0-9a-f-]{36})"')
MODEL_RE = re.compile(rb'"model":"([^"]+)"')
EFFORT_RE = re.compile(rb'"effort":"(\w+)"')
BRANCH_RE = re.compile(rb'"gitBranch":"([^"]*)"')
PMODE_RE = re.compile(rb'"permissionMode":"(\w+)"')
TOOL_RE = re.compile(rb'"type":"tool_use","id":"[^"]+","name":"([^"]+)"')

_GMTOFF = time.localtime().tm_gmtoff
_LOCAL_CACHE: dict[bytes, tuple[str, int, str]] = {}


def local_stamp(ts: bytes) -> tuple[str, int, str]:
    """(YYYY-MM-DD, час, HH:MM) в локальном времени из UTC-метки транскрипта."""
    key = ts[:16]
    hit = _LOCAL_CACHE.get(key)
    if hit:
        return hit
    try:
        dt = datetime.strptime(key.decode(), "%Y-%m-%dT%H:%M") + timedelta(seconds=_GMTOFF)
    except ValueError:
        return ("", 0, "")
    res = (dt.strftime("%Y-%m-%d"), dt.hour, dt.strftime("%H:%M"))
    if len(_LOCAL_CACHE) > 50_000:
        _LOCAL_CACHE.clear()
    _LOCAL_CACHE[key] = res
    return res


def iso_epoch(ts: bytes) -> float:
    try:
        return datetime.strptime(ts.decode(), "%Y-%m-%dT%H:%M:%S") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def empty_today() -> dict:
    return {"msgs": 0, "out": 0, "cr": 0, "cc": 0, "inp": 0, "think": 0,
            "prompts": 0, "tools": 0, "sessions": set(), "models": {},
            "hours_out": [0] * 24, "hours_msgs": [0] * 24, "minutes": Counter()}


def scan_today() -> dict:
    """Сводка по всем сессиям Claude Code за сегодня (быстрый regex-скан).

    Считает по локальному времени: часовые и минутные срезы для графиков."""
    today = date.today().isoformat()
    stats = empty_today()
    root = CLAUDE / "projects"
    if not root.is_dir():
        return stats
    cutoff = time.time() - 36 * 3600
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with f.open("rb") as fh:
                for line in fh:
                    m = TS_RE.search(line)
                    if not m:
                        continue
                    day, hour, minute = local_stamp(m.group(1))
                    if day != today:
                        continue
                    sid = SID_RE.search(line)
                    if sid:
                        stats["sessions"].add(sid.group(1))
                    if b'"tool_use"' in line:
                        stats["tools"] += 1
                    if b'"type":"user"' in line and (b'"kind":"human"' in line
                                                     or b'"promptSource"' in line):
                        stats["prompts"] += 1
                    if b'"role":"assistant"' not in line or b'"usage":' not in line:
                        continue
                    stats["msgs"] += 1
                    stats["hours_msgs"][hour] += 1
                    stats["minutes"][minute] += 1
                    for rx, key in ((OUT_RE, "out"), (CR_RE, "cr"), (CC_RE, "cc"),
                                    (IN_RE, "inp"), (THINK_RE, "think")):
                        mm = rx.search(line)
                        if mm:
                            v = int(mm.group(1))
                            stats[key] += v
                            if key == "out":
                                stats["hours_out"][hour] += v
                    mdl = MODEL_RE.search(line)
                    if mdl:
                        name = mdl.group(1).decode(errors="replace")
                        stats["models"][name] = stats["models"].get(name, 0) + 1
        except OSError:
            continue
    return stats


def recent_msgs(today: dict, minutes: int) -> int:
    """Сколько ответов ассистента за последние N минут (по минутным срезам)."""
    mins = today.get("minutes")
    if not mins:
        return 0
    now = datetime.now()
    return sum(mins.get((now - timedelta(minutes=i)).strftime("%H:%M"), 0)
               for i in range(minutes))


def pulse(today: dict, minutes: int = 60) -> list[int]:
    """Ответов в минуту за последние N минут, от старых к новым."""
    mins = today.get("minutes") or {}
    now = datetime.now()
    return [mins.get((now - timedelta(minutes=i)).strftime("%H:%M"), 0)
            for i in range(minutes - 1, -1, -1)]


def tail_bytes(path: Path, n: int = 400_000) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - n))
            return fh.read()
    except OSError:
        return b""


def transcript_path(cwd: str, sid: str) -> Path:
    enc = re.sub(r"[/.]", "-", cwd)
    return CLAUDE / "projects" / enc / f"{sid}.jsonl"


def session_context(cwd: str, sid: str) -> tuple[str, int, int]:
    """(модель, размер контекста, лимит контекста) из транскрипта сессии."""
    f = transcript_path(cwd, sid)
    if not f.is_file():
        return "—", 0, 0
    model, ctx = "—", 0
    for line in reversed(tail_bytes(f).split(b"\n")):
        if b'"role":"assistant"' in line and b'"usage":' in line:
            mdl = MODEL_RE.search(line)
            if mdl:
                model = mdl.group(1).decode(errors="replace")
            ctx = sum(int(rx.search(line).group(1)) if rx.search(line) else 0
                      for rx in (IN_RE, CR_RE, CC_RE))
            break
    # лимит контекста: метка [1m] встречается в системном промпте 1M-сессий
    head = b""
    try:
        with f.open("rb") as fh:
            head = fh.read(300_000)
    except OSError:
        pass
    limit = 1_000_000 if (b"[1m]" in head or ctx > 200_000) else 200_000
    return model, ctx, limit


def empty_session(path: Path | None = None) -> dict:
    return {"path": path, "offset": 0, "size": 0, "mtime": 0.0,
            "title": "", "prompt": "", "model": "—", "effort": "", "branch": "",
            "pmode": "", "ctx": 0, "limit": 200_000, "msgs": 0, "prompts": 0,
            "tools": 0, "last_tool": "", "out": 0, "inp": 0, "cr": 0, "cc": 0,
            "think": 0, "last_ts": 0.0, "last_prompt_ts": 0.0, "ready": False}


class SessionTracker:
    """Инкрементальная статистика по транскриптам живых сессий.

    Файл читается только с последнего известного смещения, поэтому даже
    13-мегабайтные транскрипты обходятся дёшево. Первый проход — полный."""

    def __init__(self) -> None:
        self.rec: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, sid: str) -> dict:
        return self.rec.get(sid) or empty_session()

    def update(self, cwd: str, sid: str) -> dict:
        f = transcript_path(cwd, sid)
        with self._lock:
            r = self.rec.get(sid)
            try:
                st = f.stat()
            except OSError:
                return r or empty_session(f)
            if r is None or r["path"] != f or st.st_size < r["offset"]:
                r = empty_session(f)
                head = b""
                try:
                    with f.open("rb") as fh:
                        head = fh.read(300_000)
                except OSError:
                    pass
                r["limit"] = 1_000_000 if b"[1m]" in head else 200_000
            if st.st_size > r["offset"]:
                try:
                    with f.open("rb") as fh:
                        fh.seek(r["offset"])
                        data = fh.read()
                except OSError:
                    data = b""
                nl = data.rfind(b"\n")
                if nl >= 0:
                    chunk = data[:nl + 1]
                    r["offset"] += len(chunk)
                    self._parse(r, chunk)
            r["size"], r["mtime"], r["ready"] = st.st_size, st.st_mtime, True
            if r["ctx"] > 200_000:
                r["limit"] = 1_000_000
            self.rec[sid] = dict(r)   # свежая копия — читатели видят целостный снимок
            return self.rec[sid]

    @staticmethod
    def _parse(r: dict, chunk: bytes) -> None:
        for line in chunk.split(b"\n"):
            if not line:
                continue
            if line.startswith(b'{"type":"ai-title"'):
                try:
                    r["title"] = json.loads(line).get("aiTitle", "") or r["title"]
                except ValueError:
                    pass
                continue
            if line.startswith(b'{"type":"last-prompt"'):
                try:
                    r["prompt"] = json.loads(line).get("lastPrompt", "") or r["prompt"]
                except ValueError:
                    pass
                continue
            pm = PMODE_RE.search(line)
            if pm:
                r["pmode"] = pm.group(1).decode()
            br = BRANCH_RE.search(line)
            if br:
                r["branch"] = br.group(1).decode(errors="replace")
            if b'"type":"user"' in line and (b'"kind":"human"' in line
                                             or b'"promptSource"' in line):
                r["prompts"] += 1
                ts = TSF_RE.search(line)
                if ts:
                    r["last_prompt_ts"] = iso_epoch(ts.group(1))
                continue
            if b'"role":"assistant"' not in line or b'"usage":' not in line:
                continue
            r["msgs"] += 1
            for rx, key in ((OUT_RE, "out"), (IN_RE, "inp"), (CR_RE, "cr"),
                            (CC_RE, "cc"), (THINK_RE, "think")):
                mm = rx.search(line)
                if mm:
                    r[key] += int(mm.group(1))
            r["ctx"] = sum(int(rx.search(line).group(1)) if rx.search(line) else 0
                           for rx in (IN_RE, CR_RE, CC_RE))
            mdl = MODEL_RE.search(line)
            if mdl:
                r["model"] = mdl.group(1).decode(errors="replace")
            ef = EFFORT_RE.search(line)
            if ef:
                r["effort"] = ef.group(1).decode()
            tools = TOOL_RE.findall(line)
            if tools:
                r["tools"] += len(tools)
                r["last_tool"] = tools[-1].decode(errors="replace")
            ts = TSF_RE.search(line)
            if ts:
                r["last_ts"] = iso_epoch(ts.group(1))


TRACKER = SessionTracker()


def find_agents() -> list[dict]:
    """Живые сессии Claude Code: ~/.claude/sessions/<pid>.json + проверка процесса."""
    agents = []
    sdir = CLAUDE / "sessions"
    if not sdir.is_dir():
        return agents
    for jf in sdir.glob("*.json"):
        try:
            pid = int(jf.stem)
        except ValueError:
            continue
        try:
            proc = psutil.Process(pid)
            exe = proc.exe()
        except (psutil.Error, ValueError):
            continue
        if "/claude/versions/" not in exe and "claude" not in Path(exe).name.lower():
            continue
        try:
            meta = json.loads(jf.read_text())
        except (OSError, ValueError):
            meta = {}
        agents.append({"pid": pid, "proc": proc, "meta": meta})
    agents.sort(key=lambda a: a["meta"].get("startedAt", 0))
    return agents


def mcp_servers() -> list[str]:
    try:
        cfg = json.loads((HOME / ".claude.json").read_text())
        return [f"{k} ({v.get('type', 'stdio')})"
                for k, v in (cfg.get("mcpServers") or {}).items()]
    except (OSError, ValueError):
        return []


def listening_ports() -> list[tuple[int, str, str, int, str]]:
    """[(порт, адрес, процесс, pid, подсказка)] — кто слушает TCP на этой машине.

    Сокеты читаются из /proc через psutil. Без root ядро отдаёт pid только
    для своих процессов — у чужих слушателей имя останется «—»."""
    out = []
    seen = set()
    names: dict[int, tuple[str, str]] = {}
    for c in tcp_connections():
        if c.status != psutil.CONN_LISTEN or not c.laddr:
            continue
        port_i, host, pid_i = c.laddr.port, c.laddr.ip, c.pid or 0
        key = (port_i, host, pid_i)
        if key in seen:
            continue
        seen.add(key)
        if pid_i not in names:
            name, hint = "—", ""
            try:
                p = psutil.Process(pid_i)
                name = p.name() or name
                if re.fullmatch(r"\d+\.\d+\.\d+", name):
                    name = "claude"
                if name.lower().startswith(("python", "node", "uv", "ruby", "perl", "bun")):
                    scripts = [c2 for c2 in p.cmdline()[1:] if not c2.startswith("-")]
                    if scripts:
                        hint = Path(scripts[0]).name
            except (psutil.Error, ValueError):
                pass
            names[pid_i] = (name, hint)
        name, hint = names[pid_i]
        out.append((port_i, host, name, pid_i, hint))
    out.sort()
    return out


def tcp_connections() -> list:
    """Все TCP-сокеты машины ([] если ядро не дало прочитать)."""
    try:
        return psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return []


def tcp_summary() -> dict[str, int]:
    """Счётчики TCP-соединений по состояниям (ESTABLISHED, TIME_WAIT, …)."""
    return dict(Counter(c.status for c in tcp_connections()))


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


def transcripts_size() -> tuple[int, int]:
    """(файлов, байт) во всех транскриптах Claude Code."""
    n = total = 0
    root = CLAUDE / "projects"
    if root.is_dir():
        for f in root.rglob("*.jsonl"):
            try:
                total += f.stat().st_size
                n += 1
            except OSError:
                pass
    return n, total


# ═══════════════════════════ отрисовка секций ══════════════════════════════
def render_header(console: Console, agents: list[dict] | None = None) -> Panel:
    host = platform.node()
    chip = cpu_brand()
    ram = psutil.virtual_memory().total >> 30
    up = dur(time.time() - psutil.boot_time())
    la1, la5, la15 = psutil.getloadavg()
    mem = psutil.virtual_memory()

    l1 = Text()
    l1.append(host, style=f"bold {FG}")
    facts = [(f"↑ {up}", GREEN), (datetime.now().strftime("%H:%M:%S"), FG)]
    if console.width >= 118:
        facts.insert(0, (os_label(), FG))
    if console.width >= 130:
        facts.insert(0, (f"{chip} · {ram} GB", CYAN))
    for label, style in facts:
        l1.append("  ·  ", style=DIM)
        l1.append(label, style=style)
    l2 = Text()
    l2.append("load ", style=DIM)
    l2.append(f"{la1:.2f} / {la5:.2f} / {la15:.2f}" if console.width >= 118 else f"{la1:.2f}",
              style=FG)
    l2.append("  ·  RAM ", style=DIM)
    l2.append(f"{mem.percent:.0f}%", style=FG)
    if agents is not None:
        busy = sum(1 for a in agents if a["meta"].get("status") == "busy")
        l2.append("  ·  агентов ", style=DIM)
        l2.append(str(len(agents)), style=YELLOW if agents else DIM)
        if busy:
            l2.append(f" (busy {busy})", style=YELLOW)
    if console.width >= 118:
        l2.append("  ·  Stat", style=DIM)

    grid = Table.grid(padding=(0, 2))
    wide = console.width >= 100
    logo_w = LOGO_W if wide else len(BRAND)
    grid.add_column(no_wrap=True, width=logo_w, min_width=logo_w)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    logo = logo_lines()
    if wide:
        grid.add_row(logo[0], l1)
        grid.add_row(logo[1], l2)
    else:
        grid.add_row(Text(BRAND, style=f"bold {WHITE}"), l1)
        grid.add_row(Text(""), l2)
    return Panel(grid, box=box.ROUNDED, border_style=DIM, padding=(0, 1))


def render_agents(agents: list[dict], today: dict, width: int) -> Group:
    parts: list = []
    tbl = Table(box=box.SIMPLE_HEAD, border_style=DIM, header_style=f"bold {DIM}",
                pad_edge=False, padding=(0, 1), expand=False, show_edge=False)
    # в узком терминале лишние колонки только режут содержимое многоточиями
    show_proj = width >= 140
    show_extra = width >= 112
    cols = [("", "left"), ("ИМЯ", "left"), ("ЗАДАЧА", "left")]
    if show_extra:
        cols.append(("МОДЕЛЬ", "left"))
    cols += [("АКТИВН.", "right"), ("ОТВ.", "right"), ("CPU", "right")]
    if show_extra:
        cols.append(("ПАМЯТЬ", "right"))
    cols.append(("КОНТЕКСТ", "left"))
    if show_proj:
        cols.append(("ПРОЕКТ", "left"))
    minw = {"ИМЯ": 12, "АКТИВН.": 7, "CPU": 5, "ОТВ.": 4, "КОНТЕКСТ": 18}
    for col, just in cols:
        tbl.add_column(col, justify=just, no_wrap=True, min_width=minw.get(col))

    now = time.time()
    tot_rss = tot_cpu = 0.0
    for a in agents:
        p, meta = a["proc"], a["meta"]
        status = meta.get("status") or "—"
        dot_style, label = {
            "busy": (YELLOW, "busy"),
            "idle": (GREEN, "idle"),
            "shell": (CYAN, "shell"),
        }.get(status, (DIM, status))
        # статус пишется самой сессией; если давно не обновлялся — не верим ему
        age_s = (now * 1000 - meta.get("statusUpdatedAt", 0)) / 1000
        if status != "—" and age_s > 1800:
            dot_style, label = DIM, f"{label}?"
        cwd = meta.get("cwd") or (p.cwd() if p.is_running() else "?")
        rec = TRACKER.update(cwd, meta.get("sessionId", ""))
        model, ctx, limit = rec["model"], rec["ctx"], rec["limit"]
        try:
            rss = p.memory_info().rss
            cpu = p.cpu_percent(None)
        except psutil.Error:
            rss, cpu = 0, 0.0
        tot_rss += rss
        tot_cpu += cpu

        ctx_cell = Text()
        if limit:
            ctx_cell.append_text(bar(ctx * 100 / limit, 8, lo=60, hi=80))
            ctx_cell.append(f" {htok(ctx)}", style=FG)
            ctx_cell.append(f"/{htok(limit)}", style=DIM)
        else:
            ctx_cell.append("—", style=DIM)

        dot = Text("● ", style=dot_style)
        dot.append(label, style=DIM)
        home = str(HOME)
        proj = cwd.replace(home, "~") if cwd.startswith(home) else cwd
        if len(proj) > 30:
            proj = "…" + proj[-29:]
        act = now - rec["mtime"] if rec["mtime"] else 0
        act_cell = Text(ago(act) if rec["mtime"] else "—",
                        style=GREEN if act < 60 else (FG if act < 600 else DIM))
        title_w = 36 if width >= 140 else (26 if width >= 118 else (22 if width >= 104 else 18))
        row = [dot, Text(meta.get("name", "—"), style=f"bold {CYAN}"),
               Text(clip(rec["title"] or "—", title_w), style=WHITE if rec["title"] else DIM)]
        if show_extra:
            row.append(Text(model.replace("claude-", ""), style=PURPLE))
        row += [act_cell, str(rec["msgs"]), f"{cpu:.1f}%"]
        if show_extra:
            row.append(hbytes(rss))
        row.append(ctx_cell)
        if show_proj:
            row.append(Text(proj, style=DIM))
        tbl.add_row(*row)
    parts.append(tbl)

    busy = sum(1 for a in agents if a["meta"].get("status") == "busy")
    summary = Text(" ")
    summary.append(f"{len(agents)} агент(ов)", style=f"bold {FG}")
    if busy:
        summary.append(f"  busy {busy}", style=YELLOW)
    summary.append("   ·   ", style=DIM)
    summary.append(hbytes(tot_rss), style=FG)
    summary.append(" RAM", style=DIM)
    summary.append("   ·   ", style=DIM)
    summary.append(f"{tot_cpu:.1f}%", style=FG)
    summary.append(" CPU", style=DIM)
    parts.append(summary)

    mcp = mcp_servers()
    extra = Text(" ")
    extra.append("MCP: ", style=DIM)
    extra.append("  ·  ".join(mcp) if mcp else "—", style=CYAN)
    if any(pr.info["name"] == "Claude" for pr in psutil.process_iter(["name"])):
        extra.append("     Claude Desktop: ", style=DIM)
        extra.append("запущен", style=GREEN)
    parts.append(extra)

    def stat_line(title: str, pairs) -> Text:
        t = Text(" ")
        t.append(f"{title:<9}", style=DIM)
        for i, (label, val) in enumerate(pairs):
            if i:
                t.append("   ·   ", style=DIM)
            t.append(str(val), style=FG)
            t.append(f" {label}", style=DIM)
        return t

    parts.append(stat_line("Сегодня:", (
        ("сессий", len(today["sessions"])),
        ("промптов", hnum(today["prompts"])),
        ("ответов", hnum(today["msgs"])),
        ("вызовов инструментов", hnum(today["tools"])),
    )))
    parts.append(stat_line("Токены:", (
        ("выход", htok(today["out"])),
        ("мышление", htok(today["think"])),
        ("кэш-чтение", htok(today["cr"])),
        ("кэш-запись", htok(today["cc"])),
        ("вход", htok(today["inp"])),
    )))
    parts.append(stat_line("Пульс:", (
        ("отв. за 5 мин", recent_msgs(today, 5)),
        ("за час", recent_msgs(today, 60)),
        ("пик часа", htok(max(today["hours_out"]) if today["hours_out"] else 0)),
    )))

    real = {k: v for k, v in today["models"].items() if not k.startswith("<")}
    if real:
        ml = Text(" ")
        ml.append(f"{'Модели:':<9}", style=DIM)
        top = sorted(real.items(), key=lambda x: -x[1])[:4]
        ml.append("   ·   ".join(f"{k.replace('claude-', '')} ×{v}" for k, v in top),
                  style=PURPLE)
        parts.append(ml)
    return Group(*parts)


def render_cpu(percpu: list[float], width: int) -> Group:
    total = psutil.cpu_count() or len(percpu)
    overall = sum(percpu) / len(percpu) if percpu else 0.0
    la1, la5, la15 = psutil.getloadavg()
    freq = psutil.cpu_freq()

    rows: list = [kv("Нагрузка", bar(overall, barw(width)),
                     f"{overall:>5.1f}%   load {la1:.2f} / {la5:.2f} / {la15:.2f}"
                     + (f"   {freq.current:.0f} МГц" if freq else ""))]

    cells = []
    for i, v in enumerate(percpu):
        c = Text()
        c.append(f"C{i}", style=PURPLE)
        c.append(" ")
        c.append_text(bar(v, 9))
        c.append(f" {v:>4.0f}%", style=DIM)
        cells.append(c)
    rows.append(Padding(Columns(cells, equal=True, expand=False,
                                padding=(0, 2)), (0, 0, 0, 1)))

    procs = []
    me = os.getpid()
    for p in psutil.process_iter(["name", "cpu_percent", "pid"]):
        c = p.info["cpu_percent"] or 0.0
        if c <= 0.5 or p.info["pid"] == me:
            continue
        name = p.info["name"] or "?"
        if re.fullmatch(r"\d+\.\d+\.\d+", name):   # процессы Claude Code зовутся версией
            name = "claude"
        procs.append((c, name))
    procs.sort(reverse=True)
    if procs:
        t = Text(" ")
        t.append(f"{'Топ CPU':<10}", style=DIM)
        body = "   ·   ".join(f"{n} {c:.0f}%" for c, n in procs[:5])
        limit = width - 13
        if len(body) > limit:
            body = body[:limit - 1] + "…"
        t.append(body, style=FG)
        rows.append(t)
    return Group(*rows)


def fit(t: Text, body: str, width: int, style: str = FG) -> Text:
    """Дописать body в строку t, обрезав по ширине терминала."""
    limit = max(10, width - t.cell_len - 1)
    t.append(body if len(body) <= limit else body[:limit - 1] + "…", style=style)
    return t


def render_memory(width: int) -> Group:
    m = psutil.virtual_memory()
    sw = psutil.swap_memory()
    used = m.total - m.available
    note = (f"кэш {hbytes(m.cached + m.buffers)}   swap {hbytes(sw.used)}   "
            f"свободно {hbytes(m.available)}")
    top = []
    for p in psutil.process_iter(["name", "memory_info"]):
        mi = p.info["memory_info"]
        if mi:
            name = p.info["name"] or "?"
            if re.fullmatch(r"\d+\.\d+\.\d+", name):
                name = "claude"
            top.append((mi.rss, name))
    top.sort(reverse=True)
    rows = [
        kv("Занято", bar(m.percent, barw(width)),
           f"{hbytes(used)} / {hbytes(m.total)}   {m.percent:.0f}%"),
        Text("           " + note, style=DIM),
    ]
    if top:
        t = Text(" ")
        t.append(f"{'Топ RAM':<10}", style=DIM)
        rows.append(fit(t, "   ·   ".join(f"{n} {hbytes(r)}" for r, n in top[:5]), width))
    return Group(*rows)


def render_disk(width: int) -> Group:
    rows = []
    seen: set[str] = set()
    # только реальные блочные устройства, без tmpfs и прочих псевдо-ФС
    mounts = [p for p in psutil.disk_partitions(all=False)
              if p.device.startswith("/dev/")]
    for part in mounts:
        mp = part.mountpoint
        if part.device in seen:
            continue
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        if u.total < (1 << 30):
            continue
        seen.add(part.device)
        name = "система" if mp == "/" else Path(mp).name
        rows.append(kv(name[:10], bar(u.percent, barw(width)),
                       f"{hbytes(u.used)} / {hbytes(u.total)}   "
                       f"свободно {hbytes(u.free)}"))
    return Group(*rows) if rows else Group(Text("  нет данных", style=DIM))


def battery_extra() -> str:
    """Циклы и здоровье батареи из /sys/class/power_supply (если ядро отдаёт)."""
    base = Path("/sys/class/power_supply")
    bats = sorted(base.glob("BAT*")) if base.is_dir() else []
    if not bats:
        return ""
    b = bats[0]

    def num(fn: str) -> int:
        try:
            return int((b / fn).read_text().strip())
        except (OSError, ValueError):
            return 0
    parts = []
    cyc = num("cycle_count")
    if cyc:
        parts.append(f"циклов {cyc}")
    full, design = (num("energy_full") or num("charge_full"),
                    num("energy_full_design") or num("charge_full_design"))
    if full and design:
        parts.append(f"здоровье {full * 100 / design:.0f}%")
    return ("   " + "   ".join(parts)) if parts else ""


def wifi_ssid(iface: str) -> str:
    """Имя Wi-Fi сети, если интерфейс беспроводной."""
    if not iface or not Path(f"/sys/class/net/{iface}/wireless").exists():
        return ""
    for ln in sh(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
        if ln.startswith("yes:"):
            return ln.split(":", 1)[1]
    return ""


def render_battery(width: int) -> Group | None:
    b = psutil.sensors_battery()
    if b is None:
        return None
    state = "от сети" if b.power_plugged else "разряжается"
    rem = ""
    if b.secsleft and b.secsleft > 0 and b.secsleft != psutil.POWER_TIME_UNLIMITED:
        rem = f"   осталось {b.secsleft // 3600}:{(b.secsleft % 3600) // 60:02d}"
    note = state + battery_extra()
    return Group(
        kv("Заряд", bar(b.percent, barw(width),
                        color=GREEN if b.percent > 40 else (YELLOW if b.percent > 15 else RED)),
           f"{b.percent:.0f}%{rem}"),
        Text("           " + note, style=DIM),
    )


def render_network(width: int = 118) -> Group:
    rows = []
    dif, gw_ip = default_iface()
    gw = re.match(r"(\S+)", gw_ip) if gw_ip else None
    addrs = psutil.net_if_addrs()
    ip = next((a.address for a in addrs.get(dif, [])
               if a.family.name == "AF_INET"), "—")
    t = Text(" ")
    t.append(f"{dif or 'нет маршрута':<10}", style=DIM)
    t.append(ip, style=f"bold {FG}")
    if gw:
        t.append(f"   → шлюз {gw.group(1)}", style=DIM)
    dns = dns_servers()
    if dns:
        t.append(f"   DNS {', '.join(dns)}", style=DIM)
    ssid = wifi_ssid(dif)
    if ssid:
        t.append(f"   Wi-Fi {ssid}", style=CYAN)
    rows.append(t)

    tun = Text(" ")
    tun.append(f"{'VPN':<10}", style=DIM)
    found = []
    for name, lst in addrs.items():
        if not name.startswith(VPN_PREFIXES):
            continue
        for a in lst:
            if a.family.name != "AF_INET":
                continue
            tag = ""
            if a.address.startswith(("10.8.", "10.9.")):
                tag = " vpn"
            elif a.address.startswith("100."):
                tag = " tailscale"
            found.append(f"{name} {a.address}{tag}")
    tun.append("   ·   ".join(found) if found else "туннелей нет",
               style=CYAN if found else DIM)
    rows.append(tun)

    if dif:
        rx, tx = net_counters().get(dif, (0, 0))
        if rx or tx:
            tr = Text(" ")
            tr.append(f"{'Трафик':<10}", style=DIM)
            tr.append("↓ ", style=GREEN)
            tr.append(hbytes(rx), style=FG)
            tr.append("   ↑ ", style=BLUE)
            tr.append(hbytes(tx), style=FG)
            tr.append("   с момента загрузки", style=DIM)
            rows.append(tr)

    tcp = tcp_summary()
    if tcp:
        c = Text(" ")
        c.append(f"{'TCP':<10}", style=DIM)
        c.append(f"{tcp.get('ESTABLISHED', 0)} активных", style=FG)
        c.append(f"   ·   {tcp.get('LISTEN', 0)} слушают   ·   "
                 f"{tcp.get('TIME_WAIT', 0)} time-wait", style=DIM)
        rows.append(c)
    ports = listening_ports()
    local = [p for p in ports if p[1] in ("127.0.0.1", "*", "::1", "localhost")]
    if local:
        pl = Text(" ")
        pl.append(f"{'Порты':<10}", style=DIM)
        body = "   ·   ".join(f"{port} {hint or name}" for port, _, name, _, hint in local)
        rows.append(fit(pl, body, width, CYAN))
    return Group(*rows)


# ═══════════════════════ потребление энергии ═══════════════════════════════
POWERCAP = Path("/sys/class/powercap")
HWMON = Path("/sys/class/hwmon")
# как называть источники по-человечески
POWER_NAMES = {"package-0": "CPU", "package-1": "CPU 2",
               "core": "└ ядра", "uncore": "└ графика", "dram": "└ память"}


def hwmon_power() -> list[tuple[str, float]]:
    """[(имя, ватты)] из hwmon: встроенная графика, датчики платы."""
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
            out.append((name, uw / 1e6))
    return out


# как называть датчики температуры по-человечески
TEMP_CHIPS = {"k10temp": "CPU", "coretemp": "CPU", "zenpower": "CPU",
              "amdgpu": "iGPU", "nvme": "SSD", "acpitz": "плата"}
TEMP_LABELS = {"Tctl": "", "Tdie": "", "Package id 0": "", "edge": "",
               "Composite": "", "PHY Temperature": "PHY", "MAC Temperature": "MAC"}


def temp_color(t: float, crit: float | None) -> str:
    """Зелёный / жёлтый / красный по близости к критической температуре."""
    hot = crit or 95.0
    if t >= hot * 0.92:
        return RED
    return YELLOW if t >= hot * 0.78 else GREEN


def temperatures(cards: list[dict] | None = None) -> list[tuple[str, float, float | None]]:
    """[(узел, °C, критическая °C)] по всем датчикам машины.

    Основа — hwmon через psutil (CPU, встроенная графика, сетевой чип), плюс
    температуры дискретных видеокарт из nvidia-smi."""
    out: list[tuple[str, float, float | None]] = []
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        sensors = {}
    cpu, other = [], []
    for chip, entries in sensors.items():
        base = TEMP_CHIPS.get(chip)
        if base is None:                       # сетевые чипы и прочее — по имени
            base = "сеть" if chip.startswith(("en", "eth", "wl")) else clip(chip, 9)
        for e in entries:
            label = TEMP_LABELS.get(e.label or "", e.label or "")
            if label.startswith("Tccd"):       # кристаллы (чиплеты) Ryzen
                name = f"└ чип {label[4:]}"
            else:
                name = f"{base} {label}".strip()
            row = (name, e.current, e.critical or e.high)
            (cpu if base == "CPU" else other).append(row)
    out.extend(cpu)
    for c in (cards if cards is not None else gpus()):
        out.append((f"GPU {c['idx']}", c["temp"], 90.0))
    out.extend(other)
    return out


def render_temps(width: int, cards: list[dict] | None = None) -> Group | None:
    """Секция «температуры»: сколько градусов на каждом узле."""
    rows_data = temperatures(cards)
    if not rows_data:
        return None
    rows: list = []
    for name, t, crit in rows_data:
        rows.append(kv(name, bar(t, barw(width), lo=101, hi=102,
                                 color=temp_color(t, crit)), f"{t:.0f} °C"))
    hottest = max(rows_data, key=lambda r: r[1])
    t = Text(" ")
    t.append(f"{'ГОРЯЧЕЕ':<10}", style=f"bold {DIM}")
    t.append(f"{hottest[0]} {hottest[1]:.0f} °C",
             style=temp_color(hottest[1], hottest[2]))
    t.append("   шкала 0-100 °C; дисков без smartctl не видно", style=DIM)
    rows.append(t)
    return Group(*rows)


class PowerMeter:
    """Живой замер мощности по компонентам.

    CPU считается по счётчикам энергии RAPL (`/sys/class/powercap`): ватты — это
    прирост джоулей за время между замерами. Ядро отдаёт эти файлы только root,
    поэтому при отказе в доступе CPU честно помечается как «нет доступа»."""

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

    def cpu_total(self) -> float:
        """Мощность CPU: только пакеты, чтобы не считать ядра дважды."""
        return sum(w for n, w in self.watts.items() if n.startswith("package"))


def power_breakdown(meter: PowerMeter, cards: list[dict] | None = None) -> dict:
    """Разбивка потребления: [(подпись, ватты)], сумма и что осталось неизмеренным."""
    rows: list[tuple[str, float, bool]] = list(meter.cpu_rows())
    for c in (cards if cards is not None else gpus()):
        rows.append((f"GPU {c['idx']}", c["power"], True))
    for name, w in hwmon_power():
        rows.append((name, w, True))
    return {"rows": rows, "total": sum(w for _, w, count in rows if count),
            "denied": meter.denied}


def render_power(width: int, meter: PowerMeter, cards: list[dict] | None = None) -> Group:
    """Секция «энергия»: сколько ватт ест каждый узел прямо сейчас."""
    b = power_breakdown(meter, cards)
    rows: list = []
    peak = max([w for _, w, _ in b["rows"]] or [1.0]) or 1.0
    for name, w, _ in b["rows"]:
        val = f"{w:.1f} Вт" if w >= 1 else f"{w * 1000:.0f} мВт"
        rows.append(kv(name, bar(w * 100 / peak, barw(width), lo=101, hi=102,
                                 color=CYAN), val))
    if b["denied"]:
        rows.append(Text("           CPU: нет доступа к счётчикам RAPL "
                         "(файлы energy_uj читает только root)", style=YELLOW))
    t = Text(" ")
    t.append(f"{'ИТОГО':<10}", style=f"bold {DIM}")
    t.append(f"{b['total']:.1f} Вт", style=f"bold {FG}")
    measured = "видеокарты" if b["denied"] else "CPU и видеокарты"
    t.append(f"   измерены {measured}; диски, память, плата и БП — нет", style=DIM)
    rows.append(t)
    return Group(*rows)


GPU_FIELDS = ("index", "name", "utilization.gpu", "utilization.memory",
              "memory.used", "memory.total", "temperature.gpu", "power.draw",
              "power.limit", "fan.speed", "clocks.sm", "clocks.max.sm", "uuid")


def has_nvidia() -> bool:
    return any(Path(p, "nvidia-smi").exists() for p in os.environ.get("PATH", "").split(":"))


def gpus() -> list[dict]:
    """Список видеокарт NVIDIA с загрузкой, памятью, температурой и питанием."""
    if not has_nvidia():
        return []
    out = sh(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}",
              "--format=csv,noheader,nounits"], timeout=4)
    cards = []
    for ln in out.splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) != len(GPU_FIELDS):
            continue

        def num(i, default=0.0):
            try:
                return float(f[i])
            except ValueError:
                return default
        cards.append({
            "idx": int(num(0)), "name": f[1].replace("NVIDIA ", ""),
            "util": num(2), "memutil": num(3),
            "used": num(4) * (1 << 20), "total": num(5) * (1 << 20),
            "temp": num(6), "power": num(7), "plimit": num(8),
            "fan": num(9), "clock": num(10), "clock_max": num(11), "uuid": f[12],
        })
    return cards


def gpu_procs(cards: list[dict] | None = None) -> list[dict]:
    """Процессы, занимающие видеопамять: [{pid, name, gpu, mem}]."""
    if not has_nvidia():
        return []
    by_uuid = {c["uuid"]: c["idx"] for c in (cards or gpus())}
    out = []
    for ln in sh(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid",
                  "--format=csv,noheader,nounits"], timeout=4).splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) != 3:
            continue
        try:
            pid, mem = int(f[0]), float(f[1]) * (1 << 20)
        except ValueError:
            continue
        name = "—"
        try:
            name = psutil.Process(pid).name()
            if re.fullmatch(r"\d+\.\d+\.\d+", name):
                name = "claude"
        except psutil.Error:
            pass
        out.append({"pid": pid, "name": name, "gpu": by_uuid.get(f[2], -1), "mem": mem})
    out.sort(key=lambda x: -x["mem"])
    return out


def gpu_summary(cards: list[dict]) -> str:
    """Короткая сводка для шапки: «2 × RTX 5070 Ti · 31.0 / 32.0 GB»."""
    if not cards:
        return ""
    used = sum(c["used"] for c in cards)
    total = sum(c["total"] for c in cards)
    names = sorted({c["name"] for c in cards})
    head = f"{len(cards)} × {names[0]}" if len(names) == 1 else " + ".join(names)
    return f"{head} · {hbytes(used)} / {hbytes(total)}"


def render_gpu(width: int, cards: list[dict] | None = None) -> Group | None:
    """Секция «видеокарты»: полоса загрузки, память, температура, питание."""
    cards = gpus() if cards is None else cards
    if not cards:
        return None
    rows: list = []
    for c in cards:
        mem_pct = c["used"] * 100 / c["total"] if c["total"] else 0
        rows.append(kv(f"GPU {c['idx']}", bar(c["util"], barw(width)),
                       f"{c['util']:.0f}%   {clip(c['name'], 22)}"))
        note = (f"память {hbytes(c['used'])} / {hbytes(c['total'])} ({mem_pct:.0f}%)   "
                f"{c['temp']:.0f} °C   {c['power']:.0f} / {c['plimit']:.0f} Вт")
        if c["fan"]:
            note += f"   вентилятор {c['fan']:.0f}%"
        if c["clock"]:
            note += f"   {c['clock']:.0f} МГц"
        rows.append(Text("           " + note, style=DIM))
    procs = gpu_procs(cards)
    if procs:
        t = Text(" ")
        t.append(f"{'Процессы':<10}", style=DIM)
        body = "   ·   ".join(f"{p['name']} {hbytes(p['mem'])} (GPU {p['gpu']})"
                             for p in procs[:4])
        rows.append(fit(t, body, width, CYAN))
    return Group(*rows)


def container_cli() -> str:
    """Имя доступного движка контейнеров: podman, docker или ''."""
    for cli in ("podman", "docker"):
        if any(Path(p, cli).exists() for p in os.environ.get("PATH", "").split(":")):
            return cli
    return ""


def render_containers() -> Group | None:
    cli = container_cli()
    if not cli:
        return None
    out = sh([cli, "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"], timeout=5)
    rows = [Text(" ").append(f"{cli:<10}", style=DIM)
            .append(f"{len(out.splitlines())} контейнер(ов)" if out else "нет запущенных",
                    style=GREEN if out else DIM)]
    if out:
        tbl = Table(box=box.SIMPLE, border_style=DIM, header_style=f"bold {DIM}",
                    pad_edge=False, padding=(0, 1))
        tbl.add_column("КОНТЕЙНЕР", style=CYAN, no_wrap=True)
        tbl.add_column("СТАТУС", no_wrap=True)
        tbl.add_column("ОБРАЗ", style=DIM, no_wrap=True)
        for ln in out.splitlines()[:15]:
            parts = ln.split("\t")
            if len(parts) == 3:
                up = parts[1].startswith("Up")
                tbl.add_row(parts[0], Text(parts[1], style=GREEN if up else YELLOW), parts[2])
        rows.append(tbl)
    return Group(*rows)


def rule(console: Console, emoji: str, title: str, note: str = "") -> None:
    w = console.width
    left = Text(f" {emoji}  ")
    left.append(title, style=f"bold {BLUE}")
    right = Text(f" {note} ", style=DIM) if note else Text("")
    fill = w - left.cell_len - right.cell_len - 1
    line = Text()
    line.append_text(left)
    line.append(" " + "─" * max(1, fill), style=DIM)
    line.append_text(right)
    console.print()
    console.print(line)


def build(console: Console):
    """Собрать весь вывод. Тяжёлые задачи — параллельно с замером CPU."""
    meter = PowerMeter()                       # первый замер счётчиков энергии
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_today = pool.submit(scan_today)
        agents = find_agents()
        for a in agents:                       # прайминг счётчика CPU
            try:
                a["proc"].cpu_percent(None)
            except psutil.Error:
                pass
        for p in psutil.process_iter(["name"]):
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        percpu = psutil.cpu_percent(interval=0.6, percpu=True)   # ← окно замера
        today = fut_today.result()

    w = console.width
    out = []
    out.append(("panel", render_header(console, agents)))
    out.append(("rule", ("🤖", "AI-АГЕНТЫ",
                         f"активно: {len(agents)}" if agents else "не запущены")))
    out.append(("body", render_agents(agents, today, w) if agents
                else Text("  запущенных сессий Claude Code нет", style=DIM)))
    cpu_note = f"{psutil.cpu_count(logical=False) or '?'} ядер / {psutil.cpu_count()} потоков"
    out.append(("rule", ("🧠", "ПРОЦЕССОР", cpu_note)))
    out.append(("body", render_cpu(percpu, w)))
    out.append(("rule", ("💾", "ПАМЯТЬ", hbytes(psutil.virtual_memory().total))))
    out.append(("body", render_memory(w)))
    out.append(("rule", ("💽", "ДИСК", "")))
    out.append(("body", render_disk(w)))
    batt = render_battery(w)
    if batt:
        out.append(("rule", ("🔋", "БАТАРЕЯ", "")))
        out.append(("body", batt))
    cards = gpus()
    gpu = render_gpu(w, cards)
    if gpu:
        out.append(("rule", ("🎮", "ВИДЕОКАРТЫ", gpu_summary(cards))))
        out.append(("body", gpu))
    if meter is not None:
        meter.sample()
        out.append(("rule", ("⚡", "ЭНЕРГИЯ", "прямо сейчас")))
        out.append(("body", render_power(w, meter, cards)))
    temps = render_temps(w, cards)
    if temps:
        out.append(("rule", ("🌡", "ТЕМПЕРАТУРЫ", "прямо сейчас")))
        out.append(("body", temps))
    out.append(("rule", ("🌐", "СЕТЬ", "")))
    out.append(("body", render_network(w)))
    cont = render_containers()
    if cont:
        out.append(("rule", ("📦", "КОНТЕЙНЕРЫ", container_cli())))
        out.append(("body", cont))
    return out


def emit(console: Console, out) -> None:
    for kind, payload in out:
        if kind == "rule":
            rule(console, *payload)
        else:
            console.print(payload)
    console.print()


def scan_days(days: int = 7) -> list[tuple[str, dict]]:
    """История по дням: [(дата, {msgs, out, cr, cc, tools, sessions}), ...] — для графика."""
    from collections import defaultdict
    wanted = []
    today_ord = date.today().toordinal()
    for i in range(days - 1, -1, -1):
        wanted.append(date.fromordinal(today_ord - i).isoformat())
    index = set(wanted)

    def fresh():
        return {"msgs": 0, "out": 0, "cr": 0, "cc": 0, "tools": 0, "sessions": set()}
    acc: dict[str, dict] = defaultdict(fresh)
    root = CLAUDE / "projects"
    if not root.is_dir():
        return [(d, fresh()) for d in wanted]
    cutoff = time.time() - (days + 1) * 86400
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with f.open("rb") as fh:
                for line in fh:
                    m = TS_RE.search(line)
                    if not m:
                        continue
                    day, _, _ = local_stamp(m.group(1))
                    if day not in index:
                        continue
                    bucket = acc[day]
                    sid = SID_RE.search(line)
                    if sid:
                        bucket["sessions"].add(sid.group(1))
                    if b'"tool_use"' in line:
                        bucket["tools"] += 1
                    if b'"role":"assistant"' not in line or b'"usage":' not in line:
                        continue
                    bucket["msgs"] += 1
                    for rx, key in ((OUT_RE, "out"), (CR_RE, "cr"), (CC_RE, "cc")):
                        mm = rx.search(line)
                        if mm:
                            bucket[key] += int(mm.group(1))
        except OSError:
            continue
    return [(d, acc[d]) for d in wanted]


def net_counters() -> dict[str, tuple[int, int]]:
    """{iface: (rx_bytes, tx_bytes)} — счётчики интерфейсов с момента загрузки."""
    return {n: (c.bytes_recv, c.bytes_sent)
            for n, c in psutil.net_io_counters(pernic=True).items()}


def default_iface() -> tuple[str, str]:
    """(интерфейс, шлюз) маршрута по умолчанию — из `ip route`."""
    rt = sh(["ip", "-4", "route", "show", "default"])
    m = re.search(r"\bdev\s+(\S+)", rt)
    g = re.search(r"\bvia\s+(\S+)", rt)
    return (m.group(1) if m else ""), (g.group(1) if g else "")
