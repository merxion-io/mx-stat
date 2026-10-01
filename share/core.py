# -*- coding: utf-8 -*-
"""Merxion Stat · core — сбор системных данных и статичная отрисовка через rich.

Общая база для снапшота (mx stat -s) и полноэкранного TUI (mx stat).
Всё, что зависит от ОС (Linux / macOS), вынесено в платформенный слой —
platform_linux.py и platform_macos.py, выбор в platforms.py; здесь только
общая логика: агенты, токены, сборка и отрисовка."""
from __future__ import annotations

import json
import os
import re
import sqlite3
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

from platform_common import sh
from platforms import OS

HOME = Path.home()
CLAUDE = HOME / ".claude"
CODEX = HOME / ".codex"
CODEX_TAIL = 4 << 20        # сколько хвоста rollout-а разбираем на первом проходе
CODEX_BLOCK = 8 << 20       # шаг побайтового счёта ответов
OPENCODE_DB = HOME / ".local/share/opencode/opencode.db"
# префиксы имён интерфейсов, которые считаем VPN-туннелями (у каждой ОС свои)
VPN_PREFIXES = OS.VPN_PREFIXES

# ── системные данные — из платформенного слоя, под привычными именами ──
host_name = OS.host_name
cpu_brand = OS.cpu_brand
os_label = OS.os_label
kernel_short = OS.kernel_short
machine_model = OS.machine_model
default_iface = OS.default_iface
net_counters = OS.net_counters
dns_servers = OS.dns_servers
wifi_ssid = OS.wifi_ssid
tcp_summary = OS.tcp_summary
battery_facts = OS.battery_facts
memory_extra = OS.memory_extra
cpu_summary = OS.cpu_summary
containers = OS.containers
PowerMeter = OS.PowerMeter

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
ROLLOUT_SID_RE = re.compile(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$')
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


def iso_epoch(ts: bytes | str) -> float:
    """UTC-метка ISO → epoch. Дробные секунды и хвостовой Z отбрасываются."""
    s = ts.decode(errors="replace") if isinstance(ts, bytes) else ts
    s = s.rstrip("Z").split(".")[0].split("+")[0]
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def empty_today() -> dict:
    return {"msgs": 0, "out": 0, "cr": 0, "cc": 0, "inp": 0, "think": 0,
            "prompts": 0, "tools": 0, "sessions": set(), "models": {},
            "hours_out": [0] * 24, "hours_msgs": [0] * 24, "minutes": Counter()}


def scan_today() -> dict:
    """Сводка по всем сессиям за сегодня (быстрый regex-скан).

    Считает по локальному времени: часовые и минутные срезы для графиков.
    Провайдеры складываются: claude по транскриптам, codex по rollout-ам.
    opencode сюда не попадает — он хранит только итог по сессии, без разбивки
    по дням, и приписать его токены конкретной дате нельзя."""
    today = date.today().isoformat()
    stats = empty_today()
    cutoff = time.time() - 36 * 3600
    _scan_today_codex(stats, today, cutoff)
    root = CLAUDE / "projects"
    if not root.is_dir():
        return stats
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


def codex_files(cutoff: float):
    """Rollout-транскрипты codex, тронутые не раньше cutoff."""
    sdir = CODEX / "sessions"
    if not sdir.is_dir():
        return
    for f in sdir.rglob("rollout-*.jsonl"):
        try:
            if f.stat().st_mtime >= cutoff:
                yield f
        except OSError:
            continue


def _codex_sid(f: Path) -> str:
    m = ROLLOUT_SID_RE.search(f.name)
    return m.group(1) if m else f.name


def _scan_today_codex(stats: dict, today: str, cutoff: float) -> None:
    """Дописать в сводку за сегодня ответы и токены codex.

    Один token_usage_record — один ответ модели, в его `usage` лежит дельта
    именно этого ответа (в отличие от turn_/thread_token_usage рядом)."""
    for f in codex_files(cutoff):
        sid = _codex_sid(f)
        model = ""
        try:
            with f.open("rb") as fh:
                for line in fh:
                    if b'"type":"turn_context"' in line:
                        mdl = MODEL_RE.search(line)
                        if mdl:
                            model = mdl.group(1).decode(errors="replace")
                    m = TS_RE.search(line)
                    if not m:
                        continue
                    day, hour, minute = local_stamp(m.group(1))
                    if day != today:
                        continue
                    if b'"type":"function_call"' in line or b'"custom_tool_call"' in line:
                        stats["tools"] += 1
                    if b'"role":"user"' in line and b"<environment_context>" not in line:
                        stats["prompts"] += 1
                    if b'"type":"token_usage_record"' not in line:
                        continue
                    try:
                        u = (json.loads(line).get("payload") or {}).get("usage") or {}
                    except ValueError:
                        continue
                    stats["sessions"].add(sid)
                    stats["msgs"] += 1
                    stats["hours_msgs"][hour] += 1
                    stats["minutes"][minute] += 1
                    out = u.get("output_tokens") or 0
                    stats["out"] += out
                    stats["hours_out"][hour] += out
                    stats["inp"] += u.get("input_tokens") or 0
                    stats["cr"] += u.get("cached_input_tokens") or 0
                    stats["cc"] += u.get("cache_write_input_tokens") or 0
                    stats["think"] += u.get("reasoning_output_tokens") or 0
                    if model:
                        stats["models"][model] = stats["models"].get(model, 0) + 1
        except OSError:
            continue


def _scan_days_codex(acc: dict, index: set, cutoff: float) -> None:
    for f in codex_files(cutoff):
        sid = _codex_sid(f)
        try:
            with f.open("rb") as fh:
                for line in fh:
                    m = TS_RE.search(line)
                    if not m:
                        continue
                    day, _, _ = local_stamp(m.group(1))
                    if day not in index:
                        continue
                    bucket = acc[day]
                    if b'"type":"function_call"' in line or b'"custom_tool_call"' in line:
                        bucket["tools"] += 1
                    if b'"type":"token_usage_record"' not in line:
                        continue
                    try:
                        u = (json.loads(line).get("payload") or {}).get("usage") or {}
                    except ValueError:
                        continue
                    bucket["sessions"].add(sid)
                    bucket["msgs"] += 1
                    bucket["out"] += u.get("output_tokens") or 0
                    bucket["cr"] += u.get("cached_input_tokens") or 0
                    bucket["cc"] += u.get("cache_write_input_tokens") or 0
        except OSError:
            continue


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
            "pmode": "", "status": "", "ctx": 0, "limit": 200_000,
            "msgs": 0, "prompts": 0,
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

    def update(self, cwd: str, sid: str, agent: str = "claude",
               src: str = "") -> dict:
        """Догнать транскрипт сессии. Провайдер выбирает источник и парсер."""
        if not sid:
            return empty_session()
        if agent == "opencode":
            return self._update_opencode(sid)
        if agent == "codex":
            if not src:
                return self.get(sid)
            return self._update_codex(sid, Path(src))
        if agent != "claude":
            return self.get(sid)
        return self._update_claude(cwd, sid)

    def _update_claude(self, cwd: str, sid: str) -> dict:
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

    def _update_codex(self, sid: str, f: Path) -> dict:
        """Транскрипт codex. Дальше — инкрементально, как у claude.

        Первый проход особый: rollout живой сессии доходит до десятков
        мегабайт, а разбирать его целиком в JSON слишком дорого. Счётчик
        ответов снимаем побайтовым поиском маркера по всему файлу, а
        собственно состояние (модель, контекст, статус) — только с хвоста:
        всё это перезаписывается каждым ходом, предыстория не нужна."""
        with self._lock:
            r = self.rec.get(sid)
            try:
                st = f.stat()
            except OSError:
                return r or empty_session(f)
            if r is None or r["path"] != f or st.st_size < r["offset"]:
                r = empty_session(f)
                r["limit"] = 0
                msgs, tail = 0, b""
                try:
                    with f.open("rb") as fh:
                        while True:
                            block = fh.read(CODEX_BLOCK)
                            if not block:
                                break
                            msgs += block.count(b'"type":"token_usage_record"')
                        start = max(0, st.st_size - CODEX_TAIL)
                        fh.seek(start)
                        if start:
                            fh.readline()      # добить обрезанную строку
                        tail = fh.read()
                except OSError:
                    pass
                r["offset"] = st.st_size
                if tail:
                    self._parse_codex(r, tail)
                r["msgs"] = msgs               # хвост накрутил свой счётчик
                r["size"], r["mtime"], r["ready"] = st.st_size, st.st_mtime, True
                self.rec[sid] = dict(r)
                return self.rec[sid]
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
                    self._parse_codex(r, chunk)
            r["size"], r["mtime"], r["ready"] = st.st_size, st.st_mtime, True
            self.rec[sid] = dict(r)
            return self.rec[sid]

    def _update_opencode(self, sid: str) -> dict:
        """Сессия opencode — одна строка sqlite, читать инкрементально нечего."""
        row = opencode_sessions_by_id().get(sid)
        if row is None:
            return self.get(sid)
        with self._lock:
            r = self.rec.get(sid) or empty_session()
            try:
                r["model"] = (json.loads(row["model"] or "{}").get("id")
                              or "—")
            except ValueError:
                r["model"] = "—"
            r["title"] = row["title"] or ""
            r["inp"] = row["tokens_input"] or 0
            r["out"] = row["tokens_output"] or 0
            r["cr"] = row["tokens_cache_read"] or 0
            r["cc"] = row["tokens_cache_write"] or 0
            r["think"] = row["tokens_reasoning"] or 0
            # opencode копит токены за всю сессию и не пишет размер окна —
            # показываем накопленный ввод без шкалы, врать про лимит не о чем
            r["ctx"], r["limit"] = 0, 0
            r["mtime"] = (row["time_updated"] or 0) / 1000
            r["last_ts"] = r["mtime"]
            r["ready"] = True
            self.rec[sid] = dict(r)
            return self.rec[sid]

    @staticmethod
    def _parse_codex(r: dict, chunk: bytes) -> None:
        """Записи rollout-транскрипта codex.

        Нужное лежит в четырёх видах записей: turn_context (модель),
        token_count (контекст и размер окна), task_started/task_complete
        (реальный busy/idle) и response_item с ролью user (промпты)."""
        for line in chunk.split(b"\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pay = rec.get("payload") or {}
            kind = rec.get("type")
            ptype = pay.get("type")
            if kind == "turn_context":
                r["model"] = pay.get("model") or r["model"]
                mode = (pay.get("collaboration_mode") or {}).get("settings") or {}
                r["effort"] = mode.get("reasoning_effort") or r["effort"]
                r["pmode"] = pay.get("approval_policy") or r["pmode"]
            elif kind == "event_msg" and ptype == "token_count":
                info = pay.get("info") or {}
                last = info.get("last_token_usage") or {}
                tot = info.get("total_token_usage") or {}
                r["ctx"] = last.get("input_tokens") or r["ctx"]
                r["limit"] = info.get("model_context_window") or r["limit"]
                r["inp"] = tot.get("input_tokens") or r["inp"]
                r["out"] = tot.get("output_tokens") or r["out"]
                r["cr"] = tot.get("cached_input_tokens") or r["cr"]
                r["think"] = tot.get("reasoning_output_tokens") or r["think"]
            elif kind == "token_usage_record":
                r["msgs"] += 1
            elif kind == "event_msg" and ptype in ("task_started", "task_complete",
                                                   "turn_aborted"):
                r["status"] = "busy" if ptype == "task_started" else "idle"
            elif kind == "response_item" and pay.get("role") == "user":
                txt = ""
                for c in pay.get("content") or []:
                    if c.get("type") in ("input_text", "text"):
                        txt = c.get("text") or ""
                        break
                # первый пользовательский блок каждого хода — служебный контекст
                if txt and not txt.lstrip().startswith("<environment_context>"):
                    r["prompt"] = txt.strip()
                    r["prompts"] += 1
            elif kind == "response_item" and pay.get("type") in ("function_call",
                                                                 "custom_tool_call"):
                r["tools"] += 1
                r["last_tool"] = pay.get("name") or r["last_tool"]
            ts = rec.get("timestamp")
            if ts:
                r["last_ts"] = iso_epoch(ts) or r["last_ts"]

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


# ════════════════════════ агенты: провайдеры ════════════════════════════
# Каждый провайдер отдаёт единый словарь:
#   pid, proc, agent (метка провайдера), src (путь/ключ транскрипта),
#   meta {name, status, statusUpdatedAt, cwd, sessionId, version, entrypoint, kind}
# Полнота полей разная: у claude/codex/opencode есть задача, модель и токены,
# у остальных — только сам процесс.

def agent_kind(exe: str, name: str) -> str:
    """Метка провайдера по исполняемому файлу процесса ('' — не агент)."""
    base = Path(exe).name if exe else name
    if "/claude/versions/" in exe or base == "claude":
        return "claude"
    if base == "codex":          # codex-code-mode-host и node-обёртки не в счёт
        return "codex"
    if base == "opencode":
        return "opencode"
    if base == "muse" or base.startswith("muse-bin"):
        return "muse"
    if base in ("crush", "qwen", "aider", "gemini"):
        return base
    return ""


def agent_procs() -> dict[str, list]:
    """Живые процессы агентов, разложенные по провайдерам."""
    out: dict[str, list] = {}
    for p in psutil.process_iter(["name", "exe"]):
        try:
            kind = agent_kind(p.info["exe"] or "", p.info["name"] or "")
        except (psutil.Error, TypeError):
            continue
        if kind:
            out.setdefault(kind, []).append(p)
    return out


def _proc_cwd(p) -> str:
    try:
        return p.cwd() or ""
    except psutil.Error:
        return ""


def _short_name(cwd: str, fallback: str) -> str:
    base = Path(cwd).name if cwd else ""
    return base or fallback


# ── claude ──────────────────────────────────────────────────────────────
def _claude_agents() -> list[dict]:
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
        agents.append({"pid": pid, "proc": proc, "agent": "claude",
                       "src": "", "meta": meta})
    return agents


# ── codex ───────────────────────────────────────────────────────────────
_CODEX_IDX: dict[Path, tuple[float, str, str]] = {}   # файл → (mtime, sid, cwd)


def codex_rollouts(max_age: float = 2_592_000.0) -> dict[str, dict]:
    """{session_id: {path, cwd, mtime}} по свежим rollout-транскриптам codex.

    Окно широкое (30 дней): сессию, поднятую через `codex resume`, процесс
    держит открытой сутками, а её файл всё это время не трогается.

    Первая строка файла (session_meta) читается один раз и кэшируется:
    id и cwd там неизменны, меняется только размер файла."""
    out: dict[str, dict] = {}
    sdir = CODEX / "sessions"
    if not sdir.is_dir():
        return out
    now = time.time()
    for f in sdir.rglob("rollout-*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if now - mt > max_age:
            continue
        hit = _CODEX_IDX.get(f)
        if hit is None:
            try:
                with f.open("rb") as fh:
                    meta = json.loads(fh.readline() or b"{}").get("payload", {})
            except (OSError, ValueError):
                continue
            sid = meta.get("session_id") or meta.get("id") or ""
            if not sid:
                continue
            hit = (mt, sid, meta.get("cwd") or "")
            _CODEX_IDX[f] = hit
        _, sid, cwd = hit
        prev = out.get(sid)
        if prev is None or mt > prev["mtime"]:
            out[sid] = {"path": f, "cwd": cwd, "mtime": mt}
    return out


def codex_titles() -> dict[str, str]:
    """{session_id: имя треда} из ~/.codex/session_index.jsonl."""
    out: dict[str, str] = {}
    f = CODEX / "session_index.jsonl"
    try:
        data = f.read_bytes()
    except OSError:
        return out
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id"):
            out[rec["id"]] = rec.get("thread_name") or ""
    return out


def _codex_agents(procs: list) -> list[dict]:
    rolls = codex_rollouts()
    titles = codex_titles()
    taken: set[str] = set()
    agents = []
    for p in procs:
        try:
            cmd = p.cmdline()
            started = p.create_time()
        except psutil.Error:
            continue
        cwd = _proc_cwd(p)
        sid = ""
        # `codex resume <uuid>` называет сессию прямо в аргументах — это точно
        if "resume" in cmd:
            i = cmd.index("resume")
            if len(cmd) > i + 1 and cmd[i + 1].count("-") == 4 and cmd[i + 1] in rolls:
                sid = cmd[i + 1]
        if not sid:
            # иначе берём транскрипт того же каталога, в который писали уже
            # при жизни этого процесса — значит пишет именно он
            cands = [(v["mtime"], k) for k, v in rolls.items()
                     if v["cwd"] == cwd and k not in taken and v["mtime"] >= started]
            if cands:
                sid = max(cands)[1]
        if sid:
            taken.add(sid)
        roll = rolls.get(sid, {})
        agents.append({
            "pid": p.pid, "proc": p, "agent": "codex",
            "src": str(roll.get("path", "")),
            "meta": {"name": _short_name(cwd, "codex"), "status": "",
                     "cwd": roll.get("cwd") or cwd, "sessionId": sid,
                     "version": "", "entrypoint": "cli", "kind": "cli",
                     "title": titles.get(sid, "")},
        })
    return agents


# ── opencode ────────────────────────────────────────────────────────────
def opencode_sessions() -> dict[str, dict]:
    """{directory: строка сессии} — самая свежая сессия на каждый каталог.

    Таблица session крошечная (десятки строк) даже когда файл базы весит
    сотни мегабайт, поэтому читаем её целиком и только её."""
    out: dict[str, dict] = {}
    if not OPENCODE_DB.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return out
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, directory, title, agent, model, tokens_input, tokens_output,"
            " tokens_reasoning, tokens_cache_read, tokens_cache_write, cost,"
            " time_created, time_updated from session where time_archived is null"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        con.close()
    for r in rows:
        d = dict(r)
        cur = out.get(d["directory"] or "")
        if cur is None or (d["time_updated"] or 0) > (cur["time_updated"] or 0):
            out[d["directory"] or ""] = d
    return out


def opencode_sessions_by_id() -> dict[str, dict]:
    """Те же сессии opencode, но ключом — идентификатор сессии."""
    return {r["id"]: r for r in opencode_sessions().values() if r.get("id")}


def _opencode_agents(procs: list) -> list[dict]:
    sess = opencode_sessions()
    agents = []
    for p in procs:
        cwd = _proc_cwd(p)
        row = sess.get(cwd) or {}
        agents.append({
            "pid": p.pid, "proc": p, "agent": "opencode",
            "src": row.get("id", ""),
            "meta": {"name": _short_name(cwd, "opencode"), "status": "",
                     "cwd": cwd, "sessionId": row.get("id", ""),
                     "version": "", "entrypoint": "cli", "kind": "cli",
                     "title": row.get("title") or ""},
        })
    return agents


# ── остальные CLI: видим процесс, транскрипт не разбираем ───────────────
def _plain_agents(kind: str, procs: list) -> list[dict]:
    out = []
    for p in procs:
        cwd = _proc_cwd(p)
        out.append({
            "pid": p.pid, "proc": p, "agent": kind, "src": "",
            "meta": {"name": _short_name(cwd, kind), "status": "",
                     "cwd": cwd, "sessionId": "", "version": "",
                     "entrypoint": "cli", "kind": "cli", "title": ""},
        })
    return out


def find_agents() -> list[dict]:
    """Живые AI-агенты всех поддерживаемых провайдеров."""
    procs = agent_procs()
    agents = _claude_agents()
    agents += _codex_agents(procs.get("codex", []))
    agents += _opencode_agents(procs.get("opencode", []))
    for kind, plist in procs.items():
        if kind not in ("claude", "codex", "opencode"):
            agents += _plain_agents(kind, plist)
    agents.sort(key=lambda a: (a["agent"], a["meta"].get("startedAt", 0), a["pid"]))
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

    Сокеты отдаёт платформенный слой (psutil на Linux, lsof на macOS); имя и
    подсказку (скрипт из cmdline) берём через psutil — lsof режет имена до 9
    символов. Без root ядро Linux отдаёт pid только для своих процессов —
    у чужих слушателей имя останется «—»."""
    out = []
    seen = set()
    names: dict[int, tuple[str, str]] = {}
    for port_i, host, pid_i in OS.listening_sockets():
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
    host = host_name()
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
        busy = sum(1 for a in agents if agent_status(a) == "busy")
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


def agent_status(a: dict) -> str:
    """Статус агента: собственный, если провайдер его публикует, иначе из транскрипта."""
    return (a["meta"].get("status")
            or TRACKER.get(a["meta"].get("sessionId", "")).get("status")
            or "")


def render_agents(agents: list[dict], today: dict, width: int) -> Group:
    parts: list = []
    tbl = Table(box=box.SIMPLE_HEAD, border_style=DIM, header_style=f"bold {DIM}",
                pad_edge=False, padding=(0, 1), expand=False, show_edge=False)
    # в узком терминале лишние колонки только режут содержимое многоточиями
    show_proj = width >= 140
    show_extra = width >= 112
    cols = [("", "left"), ("АГЕНТ", "left"), ("ИМЯ", "left"), ("ЗАДАЧА", "left")]
    if show_extra:
        cols.append(("МОДЕЛЬ", "left"))
    cols += [("АКТИВН.", "right"), ("ОТВ.", "right"), ("CPU", "right")]
    if show_extra:
        cols.append(("ПАМЯТЬ", "right"))
    cols.append(("КОНТЕКСТ", "left"))
    if show_proj:
        cols.append(("ПРОЕКТ", "left"))
    minw = {"АГЕНТ": 8, "ИМЯ": 12, "АКТИВН.": 7, "CPU": 5, "ОТВ.": 4,
            "КОНТЕКСТ": 18}
    for col, just in cols:
        tbl.add_column(col, justify=just, no_wrap=True, min_width=minw.get(col))

    now = time.time()
    tot_rss = tot_cpu = 0.0
    for a in agents:
        p, meta, prov = a["proc"], a["meta"], a.get("agent", "claude")
        cwd = meta.get("cwd") or (p.cwd() if p.is_running() else "?")
        rec = TRACKER.update(cwd, meta.get("sessionId", ""), prov, a.get("src", ""))
        # claude публикует статус сам, codex выводится из task_started/complete,
        # у остальных провайдеров его взять неоткуда
        status = meta.get("status") or rec.get("status") or "—"
        dot_style, label = {
            "busy": (YELLOW, "busy"),
            "idle": (GREEN, "idle"),
            "shell": (CYAN, "shell"),
        }.get(status, (DIM, status))
        # статус пишется самой сессией; если давно не обновлялся — не верим ему
        if prov == "claude":
            age_s = (now * 1000 - meta.get("statusUpdatedAt", 0)) / 1000
            if status != "—" and age_s > 1800:
                dot_style, label = DIM, f"{label}?"
        model, ctx, limit = rec["model"], rec["ctx"], rec["limit"]
        try:
            rss = p.memory_info().rss
            cpu = p.cpu_percent(None)
        except psutil.Error:
            rss, cpu = 0, 0.0
        tot_rss += rss
        tot_cpu += cpu

        ctx_cell = Text()
        if limit and rec.get("ready"):
            ctx_cell.append_text(bar(ctx * 100 / limit, 8, lo=60, hi=80))
            ctx_cell.append(f" {htok(ctx)}", style=FG)
            ctx_cell.append(f"/{htok(limit)}", style=DIM)
        elif rec.get("ready") and rec["inp"]:
            # opencode копит токены за сессию и не сообщает размер окна
            ctx_cell.append(f"{htok(rec['inp'])} вход", style=FG)
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
        title = rec["title"] or meta.get("title") or ""
        row = [dot, Text(prov, style=BLUE),
               Text(meta.get("name", "—"), style=f"bold {CYAN}"),
               Text(clip(title or "—", title_w), style=WHITE if title else DIM)]
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

    busy = sum(1 for a in agents if agent_status(a) == "busy")
    summary = Text(" ")
    summary.append(f"{len(agents)} агент(ов)", style=f"bold {FG}")
    kinds = Counter(a.get("agent", "?") for a in agents)
    if len(kinds) > 1:
        summary.append("  (" + ", ".join(f"{k} {n}" for k, n in
                                         sorted(kinds.items())) + ")", style=DIM)
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


def core_label(i: int, n: int) -> tuple[str, str]:
    """(префикс, цвет) ядра: P/E-кластеры Apple Silicon или просто C."""
    groups = OS.cpu_groups(n)
    colors = (CYAN, PURPLE) if len(groups) > 1 else (PURPLE,)
    for k, (_, prefix, idx) in enumerate(groups):
        if i in idx:
            return prefix, colors[k % len(colors)]
    return "C", PURPLE


def render_cpu(percpu: list[float], width: int) -> Group:
    overall = sum(percpu) / len(percpu) if percpu else 0.0
    la1, la5, la15 = psutil.getloadavg()
    freq = psutil.cpu_freq()

    rows: list = [kv("Нагрузка", bar(overall, barw(width)),
                     f"{overall:>5.1f}%   load {la1:.2f} / {la5:.2f} / {la15:.2f}"
                     + (f"   {freq.current:.0f} МГц" if freq else ""))]

    cells = []
    for i, v in enumerate(percpu):
        prefix, style = core_label(i, len(percpu))
        c = Text()
        c.append(f"{prefix}{i}", style=style)
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
    extra = "   ".join(f"{label} {hbytes(v)}" for label, v in memory_extra(m))
    note = f"{extra}   swap {hbytes(sw.used)}   свободно {hbytes(m.available)}"
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


def disk_volumes() -> list[tuple[str, object]]:
    """[(имя, disk_usage)] томов, которые стоит показывать (решает платформа)."""
    out = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        name = OS.disk_name(part)
        if name is None or part.device in seen:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if u.total < (1 << 30):
            continue
        seen.add(part.device)
        out.append((name, u))
    return out


def render_disk(width: int) -> Group:
    rows = []
    for name, u in disk_volumes():
        rows.append(kv(name[:10], bar(u.percent, barw(width)),
                       f"{hbytes(u.used)} / {hbytes(u.total)}   "
                       f"свободно {hbytes(u.free)}"))
    return Group(*rows) if rows else Group(Text("  нет данных", style=DIM))


def render_battery(width: int) -> Group | None:
    b = psutil.sensors_battery()
    if b is None:
        return None
    state = "от сети" if b.power_plugged else "разряжается"
    rem = ""
    if b.secsleft and b.secsleft > 0 and b.secsleft != psutil.POWER_TIME_UNLIMITED:
        rem = f"   осталось {b.secsleft // 3600}:{(b.secsleft % 3600) // 60:02d}"
    note = "   ".join([state] + [f"{k} {v}" for k, v in battery_facts()])
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


def temp_color(t: float, crit: float | None) -> str:
    """Зелёный / жёлтый / красный по близости к критической температуре."""
    hot = crit or 95.0
    if t >= hot * 0.92:
        return RED
    return YELLOW if t >= hot * 0.78 else GREEN


def temperatures(cards: list[dict] | None = None) -> list[tuple[str, float, float | None]]:
    """[(узел, °C, критическая °C)] по всем датчикам машины.

    Датчики машины отдаёт платформенный слой (hwmon на Linux, батарея на
    macOS), плюс температуры дискретных видеокарт из nvidia-smi."""
    out: list[tuple[str, float, float | None]] = []
    cpu, other = OS.temperatures()
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
    t.append(f"   шкала 0-100 °C; {OS.TEMP_NOTE}", style=DIM)
    rows.append(t)
    return Group(*rows)


def power_breakdown(meter, cards: list[dict] | None = None) -> dict:
    """Разбивка потребления: [(подпись, ватты)], сумма и что осталось неизмеренным."""
    rows: list[tuple[str, float, bool]] = list(meter.cpu_rows())
    for c in (cards if cards is not None else gpus()):
        rows.append((f"GPU {c['idx']}", c["power"], True))
    rows.extend(OS.extra_power())
    note = OS.POWER_NOTE_DENIED if meter.denied else OS.POWER_NOTE
    return {"rows": rows, "total": sum(w for _, w, count in rows if count),
            "denied": meter.denied, "denied_note": meter.denied_note, "note": note}


def render_power(width: int, meter, cards: list[dict] | None = None) -> Group:
    """Секция «энергия»: сколько ватт ест каждый узел прямо сейчас."""
    b = power_breakdown(meter, cards)
    rows: list = []
    peak = max([w for _, w, _ in b["rows"]] or [1.0]) or 1.0
    for name, w, _ in b["rows"]:
        val = f"{w:.1f} Вт" if w >= 1 else f"{w * 1000:.0f} мВт"
        rows.append(kv(name, bar(w * 100 / peak, barw(width), lo=101, hi=102,
                                 color=CYAN), val))
    if b["denied"]:
        rows.append(Text("           " + b["denied_note"][0], style=YELLOW))
    t = Text(" ")
    t.append(f"{'ИТОГО':<10}", style=f"bold {DIM}")
    t.append(f"{b['total']:.1f} Вт", style=f"bold {FG}")
    t.append(f"   {b['note'][0]}", style=DIM)
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


def render_containers(info: dict | None) -> Group | None:
    """Секция «контейнеры» по данным containers() платформенного слоя."""
    if not info:
        return None
    rows_data = info["rows"]
    if info["state"]:
        status, style = info["state"], YELLOW
    else:
        status = f"{len(rows_data)} контейнер(ов)" if rows_data else "нет запущенных"
        style = GREEN if rows_data else DIM
    rows = [Text(" ").append(f"{info['engine']:<10}", style=DIM).append(status, style=style)]
    if rows_data:
        tbl = Table(box=box.SIMPLE, border_style=DIM, header_style=f"bold {DIM}",
                    pad_edge=False, padding=(0, 1))
        tbl.add_column("КОНТЕЙНЕР", style=CYAN, no_wrap=True)
        tbl.add_column("СТАТУС", no_wrap=True)
        tbl.add_column("ОБРАЗ", style=DIM, no_wrap=True)
        for parts in rows_data[:15]:
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
    cpu_note = cpu_summary()
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
    info = containers()
    cont = render_containers(info)
    if cont:
        out.append(("rule", ("📦", "КОНТЕЙНЕРЫ", info["engine"])))
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
    cutoff = time.time() - (days + 1) * 86400
    _scan_days_codex(acc, index, cutoff)
    root = CLAUDE / "projects"
    if not root.is_dir():
        return [(d, acc[d]) for d in wanted]
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


