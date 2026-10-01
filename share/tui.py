"""Merxion Stat · tui — полноэкранный live-монитор системы и AI-агентов (Textual)."""
from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime

import psutil
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

import core
from core import (BLUE, BRAND, CYAN, DIM, FG, GREEN, LOGO, LOGO_W, PURPLE, RED,
                  WHITE, YELLOW, ago, clip, dur, hbytes, hnum, htok)

BLOCKS = "▁▂▃▄▅▆▇█"
SORT_KEYS = (("cpu", "CPU"), ("rss", "память"), ("threads", "потоки"), ("age", "возраст"))

# Колонки таблиц агентов: (ключ, заголовок, минимальная ширина экрана).
# Узкий терминал теряет второстепенные колонки, а не режет всё многоточиями.
AGENT_COLS = {
    "t-agents": (
        ("dot", "", 0), ("agent", "АГЕНТ", 88), ("name", "ИМЯ", 0),
        ("title", "ЗАДАЧА", 0), ("model", "МОДЕЛЬ", 96),
        ("ctx", "КОНТЕКСТ", 0), ("act", "АКТИВН.", 0), ("msgs", "ОТВ.", 104),
        ("cpu", "CPU", 0), ("rss", "ПАМЯТЬ", 112), ("proj", "ПРОЕКТ", 150),
    ),
    "t-agents2": (
        ("dot", "", 0), ("agent", "АГЕНТ", 88), ("name", "ИМЯ", 0),
        ("title", "ЗАДАЧА", 0), ("model", "МОДЕЛЬ", 96),
        ("branch", "ВЕТКА", 172), ("pmode", "РЕЖИМ", 182), ("entry", "ТИП", 192),
        ("msgs", "ОТВ.", 104), ("prompts", "ПРОМПТ.", 178), ("tools", "ИНСТР.", 128),
        ("out", "ВЫХОД", 120), ("ctx", "КОНТЕКСТ", 0), ("act", "АКТИВН.", 0),
        ("age", "АПТАЙМ", 150), ("cpu", "CPU", 0), ("rss", "ПАМЯТЬ", 112),
        ("pid", "PID", 160), ("proj", "ПРОЕКТ", 205),
    ),
}


def active_cols(tid: str, width: int) -> list[tuple[str, str]]:
    return [(k, h) for k, h, minw in AGENT_COLS[tid] if width >= minw]


def spark(values, width: int = 46, scale: float | None = None,
          color: str | None = None) -> Text:
    """Мини-график из блочных символов; цвет — по величине, если не задан явно."""
    vals = list(values)[-width:]
    if not vals:
        return Text("─" * width, style=DIM)
    hi = scale if scale is not None else max(vals)
    hi = hi or 1.0
    t = Text()
    if len(vals) < width:
        t.append("·" * (width - len(vals)), style=DIM)
    for v in vals:
        frac = min(1.0, max(0.0, v / hi))
        ch = BLOCKS[min(7, int(frac * 7.999))]
        if color:
            style = color
        else:
            style = GREEN if frac < 0.6 else (YELLOW if frac < 0.85 else RED)
        t.append(ch, style=style)
    return t


def hrate(bps: float) -> str:
    return f"{hbytes(bps)}/с"


def line(label: str, value, note: str = "", lw: int = 11) -> Text:
    t = Text()
    t.append(f"{label:<{lw}}", style=DIM)
    t.append_text(value if isinstance(value, Text) else Text(str(value), style=FG))
    if note:
        t.append("  ")
        t.append(note, style=DIM)
    return t


def joined(rows: list[Text], width: int | None = None) -> Text:
    """Склеить строки; при заданной ширине — обрезать каждую многоточием."""
    if width:
        for r in rows:
            r.truncate(width, overflow="ellipsis")
    return Text("\n").join(rows)


def act_style(seconds: float) -> str:
    return GREEN if seconds < 60 else (FG if seconds < 600 else DIM)


# ═══════════════════════════════ сбор данных ═══════════════════════════════
class Sampler:
    """Все замеры в одном месте; тяжёлое считается реже лёгкого."""

    HIST = 200

    def __init__(self) -> None:
        self.hist = {k: deque(maxlen=self.HIST) for k in
                     ("cpu", "mem", "rx", "tx", "dr", "dw", "pwr", "temp")}
        self.iface, self.gw = core.default_iface()
        self.prev_net = core.net_counters()
        try:
            self.prev_disk = psutil.disk_io_counters()
        except Exception:
            self.prev_disk = None
        self.prev_t = time.time()
        self.pcache: dict[int, psutil.Process] = {}
        self.tracker = core.TRACKER
        psutil.cpu_percent(None, percpu=True)
        for p in psutil.process_iter(["pid"]):
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        n = psutil.cpu_count() or 8
        self.percpu = [0.0] * n
        self.core_hist = [deque(maxlen=60) for _ in range(n)]
        psutil.cpu_times_percent(None, percpu=True)
        psutil.cpu_times_percent(None)
        self.times_core = [None] * n
        self.times_all = None
        try:
            self.cpu_stats_prev = psutil.cpu_stats()
        except Exception:
            self.cpu_stats_prev = None
        self.cpu_rates = {"ctx": 0.0, "intr": 0.0, "soft": 0.0, "sys": 0.0}
        self.kernel = core.kernel_short()
        self.mem = psutil.virtual_memory()
        self.swap = psutil.swap_memory()
        self.net_rate = (0.0, 0.0)
        self.if_rates: dict[str, tuple[float, float]] = {}
        self.disk_rate = (0.0, 0.0)
        self.net_all = self.prev_net
        self.procs: list[dict] = []
        self.agents: list[dict] = []
        self.today = core.empty_today()
        self.days: list = []
        self.transcripts = (0, 0)
        self.mcp: list[str] = core.mcp_servers()
        self.batt = None
        self.batt_extra: list[tuple[str, str]] = []
        self.batt_at = 0.0
        self.disks: list = []
        self.disks_at = 0.0
        self.containers: dict | None = None
        self.meter = core.PowerMeter()
        self.power = {"rows": [], "total": 0.0, "denied": False}
        self.temps: list[tuple[str, float, float | None]] = []
        self.gpus: list[dict] = []
        self.gpu_procs: list[dict] = []
        self.gpu_hist: dict[int, deque] = {}
        self.gpu_mem_hist: dict[int, deque] = {}
        self.ports: list = []
        self.tcp: dict[str, int] = {}
        self.dns: list[str] = []
        self.desktop = False
        self.ticks = 0

    # ── лёгкий тик, вызывается каждую секунду ──
    def tick(self) -> None:
        now = time.time()
        dt = max(0.05, now - self.prev_t)
        self.prev_t = now
        self.ticks += 1

        self.percpu = psutil.cpu_percent(None, percpu=True) or self.percpu
        self.hist["cpu"].append(sum(self.percpu) / max(1, len(self.percpu)))
        for i, v in enumerate(self.percpu[:len(self.core_hist)]):
            self.core_hist[i].append(v)
        try:
            self.times_core = psutil.cpu_times_percent(None, percpu=True)
            self.times_all = psutil.cpu_times_percent(None)
        except Exception:
            pass
        try:
            cs = psutil.cpu_stats()
            if self.cpu_stats_prev:
                pv = self.cpu_stats_prev
                self.cpu_rates = {
                    "ctx": max(0, cs.ctx_switches - pv.ctx_switches) / dt,
                    "intr": max(0, cs.interrupts - pv.interrupts) / dt,
                    "soft": max(0, cs.soft_interrupts - pv.soft_interrupts) / dt,
                    "sys": max(0, cs.syscalls - pv.syscalls) / dt,
                }
            self.cpu_stats_prev = cs
        except Exception:
            pass
        self.mem = psutil.virtual_memory()
        self.swap = psutil.swap_memory()
        self.hist["mem"].append(self.mem.percent)

        self.meter.sample()
        self.power = core.power_breakdown(self.meter, self.gpus)
        self.hist["pwr"].append(self.power["total"])
        self.temps = core.temperatures(self.gpus)
        if self.temps:
            self.hist["temp"].append(max(t for _, t, _ in self.temps))

        nc = core.net_counters()
        rates = {}
        for name, cur in nc.items():
            prev = self.prev_net.get(name, cur)
            rates[name] = (max(0, cur[0] - prev[0]) / dt, max(0, cur[1] - prev[1]) / dt)
        self.if_rates = rates
        self.net_rate = rates.get(self.iface, (0.0, 0.0))
        self.net_all, self.prev_net = nc, nc
        self.hist["rx"].append(self.net_rate[0])
        self.hist["tx"].append(self.net_rate[1])

        try:
            d = psutil.disk_io_counters()
        except Exception:
            d = None
        if d and self.prev_disk:
            self.disk_rate = (max(0, d.read_bytes - self.prev_disk.read_bytes) / dt,
                              max(0, d.write_bytes - self.prev_disk.write_bytes) / dt)
        if d:
            self.prev_disk = d
        self.hist["dr"].append(self.disk_rate[0])
        self.hist["dw"].append(self.disk_rate[1])

        self._sample_procs()
        self._sample_agents(now)
        if now - self.batt_at > 20:
            self._sample_battery()
            self.batt_at = now
        if now - self.disks_at > 30:
            self._sample_disks()
            self.disks_at = now

    def _proc(self, pid: int) -> psutil.Process | None:
        p = self.pcache.get(pid)
        if p is None or not p.is_running():
            try:
                p = psutil.Process(pid)
                p.cpu_percent(None)
            except psutil.Error:
                return None
            self.pcache[pid] = p
        return p

    def _sample_procs(self) -> None:
        me = os.getpid()
        rows = []
        alive = set()
        self.desktop = False
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info",
                                      "num_threads", "username", "create_time", "status"]):
            info = p.info
            pid = info["pid"]
            alive.add(pid)
            cpu = info["cpu_percent"] or 0.0
            mi = info["memory_info"]
            raw = info["name"] or "?"
            is_claude = raw.count(".") == 2 and raw.replace(".", "").isdigit()
            if raw == "Claude":
                self.desktop = True
            rows.append({
                "pid": pid,
                "name": "claude" if is_claude else raw,
                "claude": is_claude or raw.lower().startswith("claude"),
                "cpu": cpu,
                "rss": mi.rss if mi else 0,
                "threads": info["num_threads"] or 0,
                "user": (info["username"] or "?").split("\\")[-1],
                "age": time.time() - (info["create_time"] or time.time()),
                "status": info["status"] or "",
                "self": pid == me,
            })
        self.procs = rows
        for pid in list(self.pcache):
            if pid not in alive:
                self.pcache.pop(pid, None)

    def _sample_agents(self, now: float) -> None:
        agents = []
        for a in core.find_agents():
            pid, meta, prov = a["pid"], a["meta"], a.get("agent", "claude")
            p = self._proc(pid)
            if p is None:
                continue
            try:
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss
                age = now - p.create_time()
                threads = p.num_threads()
            except psutil.Error:
                continue
            sid = meta.get("sessionId", "")
            cwd = meta.get("cwd", "?")
            rec = self.tracker.get(sid)
            # claude пишет статус сам, codex выводится из хода, у прочих его нет
            status = meta.get("status") or rec.get("status") or "—"
            st_age = (now * 1000 - meta.get("statusUpdatedAt", 0)) / 1000
            agents.append({
                "pid": pid, "name": meta.get("name", "—"), "status": status,
                "agent": prov, "src": a.get("src", ""),
                "meta_title": meta.get("title", ""),
                "stale": prov == "claude" and status != "—" and st_age > 1800,
                "st_age": st_age, "cwd": cwd,
                "sid": sid, "version": meta.get("version", "?"),
                "kind": meta.get("kind", "?"), "entry": meta.get("entrypoint", "?"),
                "started": meta.get("startedAt", 0) / 1000,
                "cpu": cpu, "rss": rss, "age": age, "threads": threads,
                "rec": rec,
                "model": rec["model"] if rec["ready"] else "…",
                "ctx": rec["ctx"], "limit": rec["limit"],
                "act": (now - rec["mtime"]) if rec["mtime"] else -1,
            })
        self.agents = agents

    def _sample_battery(self) -> None:
        self.batt = psutil.sensors_battery()
        if self.batt is None:
            return
        self.batt_extra = core.battery_facts()

    def _sample_disks(self) -> None:
        self.disks = core.disk_volumes()

    # ── тяжёлое, в отдельных потоках ──
    def scan_slow(self) -> None:
        self.today = core.scan_today()
        self.days = core.scan_days(7)
        self.transcripts = core.transcripts_size()
        self.mcp = core.mcp_servers()

    def scan_sessions(self) -> None:
        """Догнать транскрипты живых сессий (инкрементально, дёшево)."""
        for a in list(self.agents):
            try:
                self.tracker.update(a["cwd"], a["sid"], a["agent"], a["src"])
            except Exception:
                continue

    def scan_containers(self) -> None:
        self.containers = core.containers()

    def scan_gpu(self) -> None:
        """Опрос nvidia-smi (дорогой вызов, поэтому в потоке и раз в 2 с)."""
        cards = core.gpus()
        if not cards:
            self.gpus = []
            return
        self.gpus = cards
        self.gpu_procs = core.gpu_procs(cards)
        for c in cards:
            i = c["idx"]
            self.gpu_hist.setdefault(i, deque(maxlen=self.HIST)).append(c["util"])
            mem = c["used"] * 100 / c["total"] if c["total"] else 0.0
            self.gpu_mem_hist.setdefault(i, deque(maxlen=self.HIST)).append(mem)

    def scan_net_extra(self) -> None:
        self.ports = core.listening_ports()
        self.tcp = core.tcp_summary()
        self.dns = core.dns_servers()


# ═══════════════════════════════ приложение ════════════════════════════════
class MxStat(App):
    TITLE = f"{BRAND} · Stat"
    CSS = """
    Screen { background: #16161e; color: #c0caf5; }
    #topbar { height: 2; background: #1f2335; color: #c0caf5; padding: 0 1; }
    TabbedContent { height: 1fr; }
    Tabs { background: #1f2335; }
    .row { height: auto; }
    .card {
        border: round #3b4261; padding: 0 1; height: auto;
        border-title-color: #7aa2f7; border-title-style: bold;
        border-subtitle-color: #565f89;
    }
    #c-cpu  { width: 2fr; }
    #c-mem  { width: 1fr; }
    #c-pwr  { width: 1fr; }
    #c-tok  { width: 1fr; }
    #c-net  { width: 1fr; }
    #c-dsk  { width: 1fr; }
    #c-temp { width: 1fr; }
    #t-agents, #t-agents2, #t-procs, #t-net, #t-ports { height: auto; max-height: 100%; }
    DataTable { background: #16161e; }
    DataTable > .datatable--header { background: #1f2335; color: #565f89; text-style: bold; }
    DataTable > .datatable--cursor { background: #283457; }
    Footer { background: #1f2335; }
    """

    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("1", "tab('overview')", "Обзор"),
        Binding("2", "tab('agents')", "Агенты"),
        Binding("3", "tab('procs')", "Процессы"),
        Binding("4", "tab('system')", "Система"),
        Binding("5", "tab('net')", "Сеть"),
        Binding("6", "tab('gpu')", "Видео"),
        Binding("p", "pause", "Пауза"),
        Binding("s", "sort", "Сортировка"),
        Binding("f", "filter", "Фильтр claude"),
        Binding("r", "rescan", "Пересчитать"),
        Binding("plus,equals_sign,equal", "faster", "Чаще", show=False),
        Binding("minus", "slower", "Реже", show=False),
        Binding("escape", "quit", "Выход", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.s = Sampler()
        self.paused = False
        self.interval = 1.0
        self.sort_idx = 0
        self.only_claude = False
        self.host = core.host_name() or os.uname().nodename
        self.chip = core.cpu_brand()
        self.osver = core.os_label()
        self.model_name = core.machine_model()

    # ── разметка ──
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("Обзор", id="overview"):
                with VerticalScroll():
                    with Horizontal(classes="row"):
                        yield Static(id="c-cpu", classes="card")
                        yield Static(id="c-mem", classes="card")
                        yield Static(id="c-pwr", classes="card")
                    yield DataTable(id="t-agents", classes="card")
                    yield Static(id="c-gpu", classes="card")
                    with Horizontal(classes="row"):
                        yield Static(id="c-tok", classes="card")
                        yield Static(id="c-net", classes="card")
                        yield Static(id="c-dsk", classes="card")
                        yield Static(id="c-temp", classes="card")
            with TabPane("Агенты", id="agents"):
                with VerticalScroll():
                    yield DataTable(id="t-agents2", classes="card")
                    yield Static(id="c-agent-detail", classes="card")
                    yield Static(id="c-hours", classes="card")
                    yield Static(id="c-days", classes="card")
            with TabPane("Процессы", id="procs"):
                with VerticalScroll():
                    yield DataTable(id="t-procs", classes="card")
            with TabPane("Система", id="system"):
                with VerticalScroll():
                    yield Static(id="c-sys", classes="card")
                    yield Static(id="c-claude", classes="card")
                    yield Static(id="c-disks", classes="card")
                    yield Static(id="c-cont", classes="card")
            with TabPane("Сеть", id="net"):
                with VerticalScroll():
                    yield Static(id="c-netmain", classes="card")
                    yield DataTable(id="t-net", classes="card")
                    yield DataTable(id="t-ports", classes="card")
            with TabPane("Видео", id="gpu"):
                with VerticalScroll():
                    yield Static(id="c-gpumain", classes="card")
                    yield DataTable(id="t-gpuproc", classes="card")
        yield Footer()

    def on_mount(self) -> None:
        titles = {
            "c-cpu": "🧠 ПРОЦЕССОР", "c-mem": "💾 ПАМЯТЬ", "c-pwr": "⚡ ЭНЕРГИЯ",
            "t-agents": "🤖 AI-АГЕНТЫ", "c-tok": "🎟 ТОКЕНЫ СЕГОДНЯ",
            "c-net": "🌐 СЕТЬ", "c-dsk": "💽 ДИСК", "c-temp": "🌡 ТЕМПЕРАТУРА", "t-agents2": "🤖 АГЕНТЫ",
            "c-agent-detail": "🔎 ДЕТАЛИ СЕССИИ", "c-hours": "⏰ СЕГОДНЯ ПО ЧАСАМ",
            "c-days": "📈 7 ДНЕЙ",
            "t-procs": "⚙ ПРОЦЕССЫ", "c-sys": "🖥 СИСТЕМА", "c-claude": "🤖 CLAUDE CODE",
            "c-disks": "💽 ТОМА", "c-cont": "📦 КОНТЕЙНЕРЫ", "c-netmain": "🌐 ПОДКЛЮЧЕНИЕ",
            "t-net": "🔌 ИНТЕРФЕЙСЫ", "t-ports": "🚪 ОТКРЫТЫЕ ПОРТЫ",
            "c-gpu": "🎮 ВИДЕОКАРТЫ", "c-gpumain": "🎮 ВИДЕОКАРТЫ",
            "t-gpuproc": "🧩 ПРОЦЕССЫ НА GPU",
        }
        for wid, title in titles.items():
            try:
                self.query_one(f"#{wid}").border_title = title
            except Exception:
                pass
        for tid, cols in (
            ("t-procs", ("PID", "ПРОЦЕСС", "CPU", "ПАМЯТЬ", "ПОТОКИ", "СОСТ.", "ЖИВЁТ",
                         "ПОЛЬЗОВАТЕЛЬ")),
            ("t-net", ("ИНТЕРФЕЙС", "АДРЕС", "↓ /с", "↑ /с", "ПРИНЯТО", "ОТПРАВЛЕНО", "ТИП")),
            ("t-ports", ("ПОРТ", "АДРЕС", "ПРОЦЕСС", "PID", "НАЗНАЧЕНИЕ")),
            ("t-gpuproc", ("GPU", "PID", "ПРОЦЕСС", "ВИДЕОПАМЯТЬ", "ДОЛЯ КАРТЫ", "")),
        ):
            t = self.query_one(f"#{tid}", DataTable)
            t.add_columns(*cols)
            t.zebra_stripes = True
            t.cursor_type = "row"
        self._agent_keys: dict[str, list[str]] = {}
        for tid in AGENT_COLS:
            t = self.query_one(f"#{tid}", DataTable)
            t.zebra_stripes = True
            t.cursor_type = "row"
        self._fit_agent_columns()
        self.run_worker(self.s.scan_slow, thread=True, exclusive=False)
        self.run_worker(self.s.scan_containers, thread=True, exclusive=False)
        self.run_worker(self.s.scan_net_extra, thread=True, exclusive=False)
        self.run_worker(self.s.scan_gpu, thread=True, exclusive=False)
        self._timer = self.set_interval(self.interval, self.refresh_all)
        self.set_interval(2.0, lambda: self.run_worker(self.s.scan_gpu, thread=True))
        self.set_interval(20.0, lambda: self.run_worker(self.s.scan_slow, thread=True))
        self.set_interval(3.0, lambda: self.run_worker(self.s.scan_sessions, thread=True))
        self.set_interval(10.0, lambda: self.run_worker(self.s.scan_net_extra, thread=True))
        self.set_interval(30.0, lambda: self.run_worker(self.s.scan_containers, thread=True))
        self.refresh_all()
        self.run_worker(self.s.scan_sessions, thread=True, exclusive=False)

    def _fit_agent_columns(self) -> None:
        """Пересобрать колонки таблиц агентов под текущую ширину экрана."""
        w = self.size.width or 120
        for tid in AGENT_COLS:
            cols = active_cols(tid, w)
            keys = [k for k, _ in cols]
            if self._agent_keys.get(tid) == keys:
                continue
            t = self.query_one(f"#{tid}", DataTable)
            t.clear(columns=True)
            t.add_columns(*(h for _, h in cols))
            self._agent_keys[tid] = keys

    def on_resize(self, _event) -> None:
        try:
            self._fit_agent_columns()
            self._draw_agents()
        except Exception:
            pass

    # ── действия ──
    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_pause(self) -> None:
        self.paused = not self.paused

    def action_sort(self) -> None:
        self.sort_idx = (self.sort_idx + 1) % len(SORT_KEYS)

    def action_filter(self) -> None:
        self.only_claude = not self.only_claude

    def action_rescan(self) -> None:
        self.run_worker(self.s.scan_slow, thread=True)
        self.run_worker(self.s.scan_sessions, thread=True)
        self.run_worker(self.s.scan_net_extra, thread=True)
        self.run_worker(self.s.scan_containers, thread=True)
        self.run_worker(self.s.scan_gpu, thread=True)

    def action_faster(self) -> None:
        self.interval = max(0.5, self.interval - 0.5)
        self._reschedule()

    def action_slower(self) -> None:
        self.interval = min(10.0, self.interval + 0.5)
        self._reschedule()

    def _reschedule(self) -> None:
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()
        self._timer = self.set_interval(self.interval, self.refresh_all)

    # ── отрисовка ──
    def refresh_all(self) -> None:
        if self.paused:
            self.query_one("#topbar", Static).update(self._topbar())
            return
        try:
            self.s.tick()
        except Exception:
            return
        self.query_one("#topbar", Static).update(self._topbar())
        self._draw_top_row()
        self._draw_agents()
        self._draw_tokens()
        self._draw_net_card()
        self._draw_disk_card()
        self._draw_temps()
        self._draw_hours()
        self._draw_days()
        self._draw_procs()
        self._draw_system()
        self._draw_net_tab()
        self._draw_gpu()

    def _card_width(self, wid: str, fallback: int) -> int:
        """Ширина контента карточки: Widget.size уже без рамки и отступов."""
        try:
            w = self.query_one(f"#{wid}").size.width
        except Exception:
            w = 0
        return max(16, (w or fallback) - 1)

    def _topbar(self) -> Text:
        """Двухстрочная шапка: белый логотип MERXION слева, живые метрики справа."""
        s = self.s
        w = self.size.width or 120
        cpu = s.hist["cpu"][-1] if s.hist["cpu"] else 0.0
        la = psutil.getloadavg()
        busy = sum(1 for a in s.agents if a["status"] == "busy")
        l1, l2 = Text(), Text()
        if w >= LOGO_W + 56:
            l1.append(LOGO[0], style=f"bold {WHITE}")
            l2.append(LOGO[1], style=f"bold {WHITE}")
            l1.append("  │ ", style=DIM)
            l2.append("  │ ", style=DIM)
        else:
            l1.append(f"{BRAND:<{len(BRAND) + 1}}", style=f"bold {WHITE}")
            l2.append(" " * (len(BRAND) + 1))
            l1.append("│ ", style=DIM)
            l2.append("│ ", style=DIM)

        l1.append(self.host, style=f"bold {FG}")
        facts = [(f"↑ {dur(time.time() - psutil.boot_time())}", GREEN)]
        if w >= 110:
            facts.insert(0, (self.osver, FG))
        if w >= 124:
            facts.insert(0, (self.chip, CYAN))
        for label, style in facts:
            l1.append(" · ", style=DIM)
            l1.append(label, style=style)
        l1.append(" · ", style=DIM)
        l1.append(time.strftime("%a %d.%m  %H:%M:%S" if w >= 100 else "%H:%M:%S"), style=FG)

        l2.append("CPU ", style=DIM)
        l2.append(f"{cpu:4.1f}%", style=GREEN if cpu < 60 else (YELLOW if cpu < 85 else RED))
        l2.append("  RAM ", style=DIM)
        mp = s.mem.percent
        l2.append(f"{mp:.0f}%", style=GREEN if mp < 75 else (YELLOW if mp < 90 else RED))
        if s.gpus:
            gu = max(c["util"] for c in s.gpus)
            gm = sum(c["used"] for c in s.gpus) * 100 / max(1, sum(c["total"] for c in s.gpus))
            l2.append("  GPU ", style=DIM)
            l2.append(f"{gu:.0f}%", style=GREEN if gu < 60 else (YELLOW if gu < 85 else RED))
            l2.append(f"/{gm:.0f}%", style=DIM)
        if s.temps:
            hot = max(s.temps, key=lambda x: x[1])
            l2.append("  🌡 ", style=DIM)
            l2.append(f"{hot[1]:.0f}°", style=core.temp_color(hot[1], hot[2]))
        l2.append("  load ", style=DIM)
        l2.append(f"{la[0]:.2f}", style=FG)
        l2.append("  ↓ ", style=GREEN)
        l2.append(hrate(s.net_rate[0]), style=FG)
        l2.append("  ↑ ", style=BLUE)
        l2.append(hrate(s.net_rate[1]), style=FG)
        l2.append("  🤖 ", style=DIM)
        l2.append(str(len(s.agents)), style=YELLOW if s.agents else DIM)
        if busy:
            l2.append(f" busy {busy}", style=YELLOW)
        if w >= 110:
            l2.append(f"  · {len(s.procs)} проц.", style=DIM)
        l2.append(" · ", style=DIM)
        l2.append("ПАУЗА" if self.paused else f"{self.interval:g}с",
                  style=RED if self.paused else DIM)
        return joined([l1, l2], max(20, w - 2))

    @staticmethod
    def _fit(t: Text, width: int, align: str = "left") -> Text:
        """Ячейка фиксированной ширины для табличной раскладки в Static."""
        t.truncate(width, overflow="ellipsis")
        t.align(align, width)
        return t

    def _core_line(self, i: int, bw: int, sw: int) -> Text:
        """Одна строка таблицы ядер: ЯДРО · полоса · % · user · sys · история."""
        s = self.s
        v = s.percpu[i] if i < len(s.percpu) else 0.0
        user = sysp = 0.0
        tc = s.times_core[i] if i < len(s.times_core) else None
        if tc:
            tot = tc.user + tc.nice + tc.system + tc.idle
            if tot > 0:
                user, sysp = (tc.user + tc.nice) * 100 / tot, tc.system * 100 / tot
        t = Text()
        prefix, style = core.core_label(i, len(s.percpu))
        t.append_text(self._fit(Text(f"{prefix}{i}", style=style), 5))
        t.append_text(core.bar(v, bw))
        t.append_text(self._fit(Text(f"{v:.0f}%", style=FG), 5, "right"))
        t.append_text(self._fit(Text(f"{user:.0f}%", style=DIM), 6, "right"))
        t.append_text(self._fit(Text(f"{sysp:.0f}%", style=DIM), 6, "right"))
        if sw > 0:
            t.append("  ")
            t.append_text(spark(s.core_hist[i], width=sw, scale=100))
        return t

    def _cpu_rows(self, w: int) -> list[Text]:
        s = self.s
        cpu = s.hist["cpu"][-1] if s.hist["cpu"] else 0.0
        la = psutil.getloadavg()
        freq = psutil.cpu_freq()
        n = len(s.percpu)
        rows: list[Text] = []

        # ── общая нагрузка + user/sys/idle ──
        r = line("Общая", core.bar(cpu, max(8, min(24, w - 50))), f"{cpu:5.1f}%", lw=7)
        ta = s.times_all
        if ta:
            tot = ta.user + ta.nice + ta.system + ta.idle
            if tot > 0:
                r.append(f"   user {(ta.user + ta.nice) * 100 / tot:.0f}%  "
                         f"sys {ta.system * 100 / tot:.0f}%  "
                         f"idle {ta.idle * 100 / tot:.0f}%", style=DIM)
        rows.append(r)
        rows.append(spark(s.hist["cpu"], width=w, scale=100))
        rows.append(Text(""))

        # ── таблица ядер: одна или две колонки по ширине ──
        # группы ядер решает платформа: P/E-кластеры на Apple Silicon,
        # одна группа на x86 — тогда две колонки просто пополам
        groups = core.OS.cpu_groups(n)
        if len(groups) > 1:
            (p_name, _, p_idx), (e_name, _, e_idx) = groups[0], groups[1]
        else:
            half = (n + 1) // 2
            p_name = e_name = groups[0][0]
            p_idx, e_idx = list(range(half)), list(range(half, n))
        two = bool(w >= 86 and p_idx and e_idx)
        colw = (w - 3) // 2 if two else w
        fixed = 5 + 5 + 6 + 6
        bw = max(6, min(16, colw - fixed - 12))
        sw = max(0, min(24, colw - fixed - bw - 2))

        def header() -> Text:
            h = Text()
            h.append_text(self._fit(Text("ЯДРО", style=f"bold {DIM}"), 5))
            h.append_text(self._fit(Text("НАГРУЗКА", style=f"bold {DIM}"), bw))
            h.append_text(self._fit(Text("%", style=f"bold {DIM}"), 5, "right"))
            h.append_text(self._fit(Text("USER", style=f"bold {DIM}"), 6, "right"))
            h.append_text(self._fit(Text("SYS", style=f"bold {DIM}"), 6, "right"))
            if sw > 0:
                h.append("  ")
                h.append_text(self._fit(Text("ИСТОРИЯ", style=f"bold {DIM}"), sw))
            return h

        def cluster(label: str, idx: list[int], style: str) -> Text:
            vals = [s.percpu[i] for i in idx if i < len(s.percpu)]
            avg = sum(vals) / len(vals) if vals else 0.0
            peak = max(vals) if vals else 0.0
            t = Text()
            t.append(f"{label} ", style=style)
            t.append(f"{len(idx)} ядер · ср. {avg:.0f}% · пик {peak:.0f}%", style=DIM)
            return t

        if two:
            left = [header()] + [self._core_line(i, bw, sw) for i in p_idx] + \
                   [cluster(p_name, p_idx, CYAN)]
            right = [header()] + [self._core_line(i, bw, sw) for i in e_idx] + \
                    [cluster(e_name, e_idx, PURPLE)]
            for k in range(max(len(left), len(right))):
                merged = Text()
                l = left[k] if k < len(left) else Text("")
                rr = right[k] if k < len(right) else Text("")
                merged.append_text(self._fit(l, colw))
                merged.append("   ")
                merged.append_text(rr)
                rows.append(merged)
        else:
            rows.append(header())
            rows += [self._core_line(i, bw, sw) for i in range(n)]
            if len(groups) > 1:
                rows.append(cluster(p_name, p_idx, CYAN))
                rows.append(cluster(e_name, e_idx, PURPLE))
            else:
                rows.append(cluster("все ядра", list(range(n)), CYAN))
        rows.append(Text(""))

        # ── потоки и ядро ОС ──
        threads = sum(p["threads"] for p in s.procs)
        agent_threads = sum(a["threads"] for a in s.agents)
        per_core = threads / max(1, n)
        rows.append(line("load", f"{la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}",
                         (f"{freq.current:.0f} МГц  ·  " if freq else "")
                         + core.cpu_summary(),
                         lw=8))
        rows.append(line("потоки", f"{hnum(threads)}",
                         f"процессов {len(s.procs)}  ·  {per_core:.0f} на ядро  ·  "
                         f"claude {agent_threads}", lw=8))
        cr = s.cpu_rates
        rows.append(line("ядро ОС", f"сисколлов {htok(cr['sys'])}/с",
                         f"soft-прерываний {htok(cr['soft'])}/с", lw=8))
        rows.append(Text(""))

        # ── топ процессов таблицей ──
        namew = max(12, min(28, w - 36))
        h = Text()
        h.append_text(self._fit(Text("ТОП ПО CPU", style=f"bold {DIM}"), namew))
        h.append_text(self._fit(Text("CPU", style=f"bold {DIM}"), 7, "right"))
        h.append_text(self._fit(Text("ПОТОКИ", style=f"bold {DIM}"), 8, "right"))
        h.append_text(self._fit(Text("ПАМЯТЬ", style=f"bold {DIM}"), 9, "right"))
        h.append_text(self._fit(Text("PID", style=f"bold {DIM}"), 7, "right"))
        rows.append(h)
        top = sorted((p for p in s.procs if not p["self"]), key=lambda p: -p["cpu"])[:5]
        for p in top:
            t = Text()
            t.append_text(self._fit(Text(p["name"], style=CYAN if p["name"] == "claude" else FG),
                                    namew))
            t.append_text(self._fit(Text(f"{p['cpu']:.1f}%",
                                         style=RED if p["cpu"] > 50 else
                                         YELLOW if p["cpu"] > 15 else FG), 7, "right"))
            t.append_text(self._fit(Text(str(p["threads"]), style=DIM), 8, "right"))
            t.append_text(self._fit(Text(hbytes(p["rss"]), style=DIM), 9, "right"))
            t.append_text(self._fit(Text(str(p["pid"]), style=DIM), 7, "right"))
            rows.append(t)
        return rows

    def _mem_rows(self, w: int) -> list[Text]:
        s = self.s
        m, sw = s.mem, s.swap
        bw = max(6, min(14, w - 14))
        rows = [
            line("Занято", core.bar(m.percent, bw), f"{m.percent:.0f}%", lw=8),
            Text(""),
            spark(s.hist["mem"], width=w, scale=100),
            Text(""),
            line("исп.", f"{hbytes(m.total - m.available)} / {hbytes(m.total)}", lw=10),
            line("активная", hbytes(m.active), lw=10),
            *(line(label, hbytes(v), lw=10) for label, v in core.memory_extra(m)),
            line("свободно", hbytes(m.available), lw=10),
            line("swap", f"{hbytes(sw.used)} / {hbytes(sw.total)}", lw=10),
            Text(""),
            line("ТОП RAM", "", lw=10),
        ]
        rows[-1].stylize(f"bold {DIM}")
        top = sorted(s.procs, key=lambda p: -p["rss"])[:5]
        for p in top:
            rows.append(line(clip(p["name"], 9), hbytes(p["rss"]), lw=10))
        return rows

    def _pwr_rows(self, w: int) -> list[Text]:
        s = self.s
        rows: list[Text] = []

        # ── живое потребление по узлам ──
        pw = s.power
        peak = max([v for _, v, _ in pw["rows"]] or [1.0]) or 1.0
        vw = 9                                      # «278.7 Вт» + отступ
        bw = min(14, w - vw - 12) if w >= 27 else 0  # на узкой карточке полосы нет
        lw = max(8, w - vw - bw)
        for name, watts, _ in pw["rows"]:
            val = f"{watts:.1f} Вт" if watts >= 1 else f"{watts * 1000:.0f} мВт"
            t = Text()
            t.append(f"{clip(name, lw - 1):<{lw}}", style=DIM)
            if bw:
                t.append_text(core.bar(watts * 100 / peak, bw, lo=101, hi=102, color=CYAN))
            t.append_text(self._fit(Text(val, style=FG), vw, "right"))
            rows.append(t)
        if pw["denied"]:
            rows.append(Text(pw["denied_note"][1], style=YELLOW))
        tot = Text()
        tot.append(f"{'ИТОГО':<{lw}}", style=f"bold {DIM}")
        tot.append_text(self._fit(Text(f"{pw['total']:.1f} Вт", style=f"bold {FG}"),
                                  max(8, bw + vw), "right"))
        rows.append(tot)
        g = Text()
        note = "  ~3 мин" if w >= 30 else ""
        g.append(f"{'график':<{lw}}", style=DIM)
        g.append_text(spark(s.hist["pwr"], width=max(8, w - lw - len(note)), color=CYAN))
        g.append(note, style=DIM)
        rows.append(g)
        note = pw.get("note") or core.OS.POWER_NOTE
        rows.append(Text(note[1] if w >= 30 else note[2], style=DIM))
        rows.append(Text(""))

        if s.batt:
            b = s.batt
            colr = GREEN if b.percent > 40 else (YELLOW if b.percent > 15 else RED)
            rows.append(line("Заряд", core.bar(b.percent, max(6, min(14, w - 14)), color=colr),
                             f"{b.percent:.0f}%", lw=8))
            rows.append(Text(""))
            state = "от сети" if b.power_plugged else "разряжается"
            rows.append(line("режим", state, lw=12))
            if b.secsleft and b.secsleft > 0 and b.secsleft != psutil.POWER_TIME_UNLIMITED:
                rows.append(line("осталось", f"{b.secsleft // 3600}:{(b.secsleft % 3600) // 60:02d}", lw=12))
            for label, val in s.batt_extra:
                rows.append(line(label, val, lw=12))
        else:
            rows.append(Text("батарея не найдена", style=DIM))
        rows.append(Text(""))
        rows.append(line("процессов", len(s.procs), lw=12))
        rows.append(line("потоков", sum(p["threads"] for p in s.procs), lw=12))
        rows.append(line("TCP", f"{s.tcp.get('ESTABLISHED', 0)} соед.",
                         f"{s.tcp.get('LISTEN', 0)} слуш.", lw=12))
        rows.append(line("загружена",
                         time.strftime("%d.%m %H:%M", time.localtime(psutil.boot_time())), lw=12))
        rows.append(line("аптайм", dur(time.time() - psutil.boot_time()), lw=12))
        n, total = s.transcripts
        if n:
            rows.append(line("транскр.", hbytes(total), f"{n} ф.", lw=12))
        return rows

    def _draw_top_row(self) -> None:
        """Три верхние карточки одной высоты: короткие добиваются пустыми строками."""
        cards = []
        for wid, fallback, builder in (("c-cpu", 74, self._cpu_rows),
                                       ("c-mem", 36, self._mem_rows),
                                       ("c-pwr", 36, self._pwr_rows)):
            w = self._card_width(wid, fallback)
            cards.append((wid, w, builder(w)))
        height = max(len(rows) for _, _, rows in cards)
        for wid, w, rows in cards:
            rows += [Text("")] * (height - len(rows))
            self.query_one(f"#{wid}", Static).update(joined(rows, w))

    # ── агенты ──
    def _agent_cells(self, a: dict, title_w: int) -> dict:
        dot_style, label = {"busy": (YELLOW, "busy"),
                            "idle": (GREEN, "idle"),
                            "shell": (CYAN, "shell"),
                            "waiting": (BLUE, "waiting")}.get(a["status"],
                                                             (DIM, a["status"]))
        if a["stale"]:
            dot_style, label = DIM, f"{label}?"
        dot = Text("● ", style=dot_style)
        dot.append(label, style=DIM)
        rec = a["rec"]
        ctx = Text()
        if rec["ready"] and a["limit"]:
            ctx.append_text(core.bar(a["ctx"] * 100 / max(1, a["limit"]), 8, lo=60, hi=80))
            ctx.append(f" {htok(a['ctx'])}", style=FG)
            ctx.append(f"/{htok(a['limit'])}", style=DIM)
        elif rec["ready"] and rec["inp"]:
            # opencode копит токены за сессию и не сообщает размер окна
            ctx.append(f"{htok(rec['inp'])} вход", style=FG)
        elif a["sid"]:
            ctx.append("…", style=DIM)
        else:
            ctx.append("—", style=DIM)
        proj = a["cwd"].replace(str(core.HOME), "~")
        if len(proj) > 30:
            proj = "…" + proj[-29:]
        title = rec["title"] or a.get("meta_title") or ""
        return {
            "dot": dot,
            "agent": Text(a.get("agent", "?"), style=BLUE),
            "name": Text(a["name"], style=f"bold {CYAN}"),
            "title": Text(clip(title, title_w) if title else "—",
                          style=WHITE if title else DIM),
            "model": Text(a["model"].replace("claude-", ""), style=PURPLE),
            "branch": Text(clip(rec["branch"] or "—", 18), style=FG if rec["branch"] else DIM),
            "pmode": Text(rec["pmode"] or "—",
                          style=YELLOW if rec["pmode"] == "bypassPermissions" else DIM),
            "entry": Text({"cli": "cli", "sdk-cli": "sdk"}.get(a["entry"], a["entry"]), style=DIM),
            "msgs": str(rec["msgs"]),
            "prompts": str(rec["prompts"]),
            "tools": str(rec["tools"]),
            "out": htok(rec["out"]),
            "ctx": ctx,
            "act": (Text(ago(a["act"]), style=act_style(a["act"])) if a["act"] >= 0
                    else Text("—", style=DIM)),
            "age": dur(a["age"]),
            "cpu": f"{a['cpu']:.1f}%",
            "rss": hbytes(a["rss"]),
            "pid": str(a["pid"]),
            "proj": Text(proj, style=DIM),
        }

    def _draw_agents(self) -> None:
        w = self.size.width or 120
        title_w = 36 if w >= 160 else (28 if w >= 130 else (22 if w >= 100 else 16))
        for tid in AGENT_COLS:
            keys = self._agent_keys.get(tid) or [k for k, _ in active_cols(tid, w)]
            t = self.query_one(f"#{tid}", DataTable)
            cursor = t.cursor_row
            t.clear()
            for a in self.s.agents:
                cells = self._agent_cells(a, title_w)
                t.add_row(*(cells[k] for k in keys))
            if self.s.agents:
                t.move_cursor(row=min(cursor, len(self.s.agents) - 1))
        ag = self.s.agents
        tot_rss = sum(a["rss"] for a in ag)
        tot_cpu = sum(a["cpu"] for a in ag)
        busy = sum(1 for a in ag if a["status"] == "busy")
        tot_out = sum(a["rec"]["out"] for a in ag)
        sub = (f"{len(ag)} активных · busy {busy} · {hbytes(tot_rss)} RAM · "
               f"{tot_cpu:.1f}% CPU · {htok(tot_out)} вых. токенов за сессии"
               if ag else "нет активных сессий")
        self.query_one("#t-agents").border_subtitle = sub
        self.query_one("#t-agents2").border_subtitle = sub
        self._draw_agent_detail()

    def _draw_agent_detail(self) -> None:
        t = self.query_one("#t-agents2", DataTable)
        idx = t.cursor_row if self.s.agents else -1
        w = self._card_width("c-agent-detail", 120)
        if not self.s.agents or idx < 0 or idx >= len(self.s.agents):
            self.query_one("#c-agent-detail", Static).update(
                Text("нет данных", style=DIM))
            return
        a = self.s.agents[idx]
        rec = a["rec"]
        pct = a["ctx"] * 100 / max(1, a["limit"])
        act = ago(a["act"]) + " назад" if a["act"] >= 0 else "—"
        last_ans = (ago(time.time() - rec["last_ts"]) + " назад" if rec["last_ts"] else "—")
        last_q = (ago(time.time() - rec["last_prompt_ts"]) + " назад"
                  if rec["last_prompt_ts"] else "—")
        cache_in = rec["cr"] + rec["cc"] + rec["inp"]
        hit = f"{rec['cr'] * 100 / cache_in:.0f}%" if cache_in else "—"
        lw = 15
        rows = [
            line("задача", Text(rec["title"] or "—", style=f"bold {WHITE}"), lw=lw),
            line("посл. запрос", Text(clip(rec["prompt"], max(20, w - lw - 2)) or "—",
                                      style=FG), lw=lw),
            line("сессия", a["sid"], f"{a['name']}  ·  {a['kind']} / {a['entry']}  ·  v{a['version']}",
                 lw=lw),
            line("модель", Text(a["model"], style=PURPLE),
                 f"effort {rec['effort'] or '—'}  ·  режим {rec['pmode'] or '—'}", lw=lw),
            line("статус", f"{a['status']}  ({ago(a['st_age'])} назад)",
                 f"активность в транскрипте {act}  ·  ответ {last_ans}  ·  промпт {last_q}",
                 lw=lw),
            line("папка", a["cwd"], f"ветка {rec['branch'] or '—'}", lw=lw),
            line("запущена", time.strftime("%d.%m %H:%M", time.localtime(a["started"])),
                 f"живёт {dur(a['age'])}", lw=lw),
            line("контекст", core.bar(pct, 24, lo=60, hi=80),
                 f"{hnum(a['ctx'])} / {hnum(a['limit'])} ток. ({pct:.0f}%)", lw=lw),
            line("за сессию", f"{rec['msgs']} ответов  ·  {rec['prompts']} промптов  ·  "
                              f"{rec['tools']} инструментов",
                 f"последний инструмент {rec['last_tool'] or '—'}", lw=lw),
            line("токены", f"выход {htok(rec['out'])}  ·  мышление {htok(rec['think'])}  ·  "
                           f"вход {htok(rec['inp'])}  ·  кэш-чт {htok(rec['cr'])}  ·  "
                           f"кэш-зп {htok(rec['cc'])}", f"кэш-хит {hit}", lw=lw),
            line("ресурсы", f"CPU {a['cpu']:.1f}%   RAM {hbytes(a['rss'])}   "
                            f"потоков {a['threads']}   PID {a['pid']}", lw=lw),
            line("транскрипт", str(rec["path"]).replace(str(core.HOME), "~") if rec["path"] else "—",
                 hbytes(rec["size"]) if rec["size"] else "", lw=lw),
        ]
        self.query_one("#c-agent-detail", Static).update(joined(rows, w))

    def on_data_table_row_highlighted(self, event) -> None:
        try:
            if event.data_table.id == "t-agents2":
                self._draw_agent_detail()
        except Exception:
            pass

    def _draw_tokens(self) -> None:
        d = self.s.today
        w = self._card_width("c-tok", 48)
        cache_in = d["cr"] + d["cc"] + d["inp"]
        hit = f"{d['cr'] * 100 / cache_in:.0f}%" if cache_in else "—"
        rows = [
            line("сессий", hnum(len(d["sessions"])), f"промптов {hnum(d['prompts'])}", lw=12),
            line("ответов", hnum(d["msgs"]), f"инструментов {hnum(d['tools'])}", lw=12),
            Text(""),
            line("выход", htok(d["out"]), f"мышление {htok(d['think'])}", lw=12),
            line("кэш-чтение", htok(d["cr"]), f"кэш-хит {hit}", lw=12),
            line("кэш-запись", htok(d["cc"]), f"вход {htok(d['inp'])}", lw=12),
            Text(""),
            line("пульс 60м", "", lw=12),
            spark(core.pulse(d, 60), width=w, color=CYAN),
            line("за 5 мин", f"{core.recent_msgs(d, 5)} отв.",
                 f"за час {core.recent_msgs(d, 60)}", lw=12),
        ]
        real = {k: v for k, v in d["models"].items() if not k.startswith("<")}
        if real:
            rows.append(Text(""))
            for k, v in sorted(real.items(), key=lambda x: -x[1])[:3]:
                rows.append(line(k.replace("claude-", "")[:11], f"×{v}", lw=12))
        self.query_one("#c-tok", Static).update(joined(rows, w))

    def _draw_net_card(self) -> None:
        s = self.s
        w = self._card_width("c-net", 48)
        addrs = psutil.net_if_addrs()
        ip = next((a.address for a in addrs.get(s.iface, [])
                   if a.family.name == "AF_INET"), "—")
        tot = s.net_all.get(s.iface, (0, 0))
        rows = [
            line(s.iface or "—", ip, f"шлюз {s.gw or '—'}", lw=8),
            line("DNS", ", ".join(s.dns) or "—", lw=8),
            Text(""),
            line("↓", hrate(s.net_rate[0]), lw=3),
            spark(s.hist["rx"], width=w, color=GREEN),
            line("↑", hrate(s.net_rate[1]), lw=3),
            spark(s.hist["tx"], width=w, color=BLUE),
            Text(""),
            line("всего ↓", hbytes(tot[0]), lw=8),
            line("всего ↑", hbytes(tot[1]), lw=8),
            line("TCP", f"{s.tcp.get('ESTABLISHED', 0)} соед.",
                 f"{s.tcp.get('LISTEN', 0)} портов", lw=8),
        ]
        vpn = []
        for name, lst in addrs.items():
            if not name.startswith(core.VPN_PREFIXES):
                continue
            for a in lst:
                if a.family.name != "AF_INET":
                    continue
                tag = ("vpn" if a.address.startswith(("10.8.", "10.9."))
                       else "tailscale" if a.address.startswith("100.")
                       else name)
                vpn.append(tag)
        if vpn:
            rows.append(line("VPN", ", ".join(sorted(set(vpn))), lw=8))
        self.query_one("#c-net", Static).update(joined(rows, w))

    def _draw_disk_card(self) -> None:
        s = self.s
        w = self._card_width("c-dsk", 48)
        rows = []
        for name, u in s.disks:
            rows.append(line(name[:8], core.bar(u.percent, 12), f"{u.percent:.0f}%", lw=9))
            rows.append(line("", f"{hbytes(u.used)} / {hbytes(u.total)}", lw=9))
            rows.append(line("своб.", hbytes(u.free), lw=9))
        rows += [
            Text(""),
            line("чтение", hrate(s.disk_rate[0]), lw=9),
            spark(s.hist["dr"], width=w, color=CYAN),
            line("запись", hrate(s.disk_rate[1]), lw=9),
            spark(s.hist["dw"], width=w, color=PURPLE),
        ]
        n, total = s.transcripts
        if n:
            rows.append(Text(""))
            rows.append(line("транскр.", hbytes(total), f"{n} файлов", lw=9))
        self.query_one("#c-dsk", Static).update(joined(rows, w))

    def _draw_hours(self) -> None:
        """Гистограмма выходных токенов по часам текущего дня (24 столбца)."""
        d = self.s.today
        out, msgs = d["hours_out"], d["hours_msgs"]
        w = self._card_width("c-hours", 120)
        if not any(out):
            self.query_one("#c-hours", Static).update(Text("сегодня ещё пусто", style=DIM))
            return
        peak = max(out) or 1
        cur = datetime.now().hour
        H = 5
        rows = []
        for level in range(H, 0, -1):
            t = Text(f"{htok(peak * level / H):>6} " if level in (H, 1) else " " * 7, style=DIM)
            for h in range(24):
                frac = out[h] / peak * H
                ch = "██" if frac >= level else ("▄▄" if frac >= level - 0.5 else "  ")
                style = (CYAN if h == cur else
                         GREEN if out[h] / peak < 0.6 else
                         YELLOW if out[h] / peak < 0.85 else RED)
                t.append(ch, style=style)
                t.append(" ")
            rows.append(t)
        lbl = Text(" " * 7)
        for h in range(24):
            lbl.append(f"{h:02d} ", style=f"bold {CYAN}" if h == cur else DIM)
        rows.append(lbl)
        best = max(range(24), key=lambda h: out[h])
        summary = Text()
        summary.append("сейчас ", style=DIM)
        summary.append(f"{cur:02d}ч", style=CYAN)
        summary.append(f"  {htok(out[cur])} вых. · {msgs[cur]} отв.", style=FG)
        summary.append("     пик ", style=DIM)
        summary.append(f"{best:02d}ч", style=FG)
        summary.append(f"  {htok(out[best])} вых. · {msgs[best]} отв.", style=FG)
        summary.append(f"     активных часов {sum(1 for v in msgs if v)}", style=DIM)
        rows.append(Text(""))
        rows.append(summary)
        self.query_one("#c-hours", Static).update(joined(rows, w))

    def _draw_days(self) -> None:
        if not self.s.days:
            self.query_one("#c-days", Static).update(Text("считаю…", style=DIM))
            return
        peak = max((v["out"] for _, v in self.s.days), default=1) or 1
        w = self._card_width("c-days", 120)
        bw = max(12, min(44, w - 50))
        rows = []
        tot_out = tot_msgs = 0
        for day, v in self.s.days:
            frac = v["out"] / peak
            barw = int(frac * bw)
            t = Text()
            t.append(f"{day[5:]}  ", style=DIM)
            t.append("█" * barw, style=GREEN if frac < 0.6 else
                     (YELLOW if frac < 0.85 else RED))
            t.append("░" * (bw - barw), style=DIM)
            t.append(f"  {htok(v['out']):>6}", style=FG)
            t.append(f"  {v['msgs']:>5} отв.  {v['tools']:>5} инстр.  "
                     f"{len(v['sessions']):>2} сес.", style=DIM)
            rows.append(t)
            tot_out += v["out"]
            tot_msgs += v["msgs"]
        self.query_one("#c-days", Static).update(joined(rows, w))
        self.query_one("#c-days").border_subtitle = (
            f"выходных токенов по дням · за 7 дней {htok(tot_out)} / {hnum(tot_msgs)} отв.")

    def _draw_procs(self) -> None:
        t = self.query_one("#t-procs", DataTable)
        cursor = t.cursor_row
        t.clear()
        key, label = SORT_KEYS[self.sort_idx]
        src = self.s.procs
        if self.only_claude:
            src = [p for p in src if p["claude"]]
        rows = sorted(src, key=lambda p: -p[key])[:60]
        for p in rows:
            name = Text(p["name"], style=CYAN if p["name"] == "claude" else FG)
            cpu = Text(f"{p['cpu']:.1f}%",
                       style=RED if p["cpu"] > 50 else YELLOW if p["cpu"] > 15 else FG)
            st = {"running": "run", "sleeping": "sleep", "idle": "idle",
                  "stopped": "stop", "zombie": "zombie"}.get(p["status"], p["status"][:6])
            t.add_row(str(p["pid"]), name, cpu, hbytes(p["rss"]),
                      str(p["threads"]), Text(st, style=DIM), dur(p["age"]), p["user"])
        if rows:
            t.move_cursor(row=min(cursor, len(rows) - 1))
        tot_cpu = sum(p["cpu"] for p in src)
        tot_rss = sum(p["rss"] for p in src)
        t.border_subtitle = (f"{len(src)} проц. · {tot_cpu:.0f}% CPU · {hbytes(tot_rss)} RSS · "
                             f"сортировка: {label} (s) · "
                             f"фильтр: {'только claude' if self.only_claude else 'все'} (f)")

    def _draw_system(self) -> None:
        s = self.s
        m = s.mem
        boot = time.strftime("%d.%m.%Y %H:%M", time.localtime(psutil.boot_time()))
        w = self._card_width("c-sys", 120)
        rows = [
            line("машина", f"{self.host}  ·  {self.chip}  ·  {self.model_name}", lw=14),
            line("ядра", f"{core.cpu_summary(short=True)}  (всего {psutil.cpu_count()})",
                 lw=14),
            line("система", f"{self.osver}  ·  {s.kernel}", lw=14),
            line("загружена", f"{boot}  ·  аптайм {dur(time.time() - psutil.boot_time())}", lw=14),
            line("память", f"{hbytes(m.total - m.available)} из {hbytes(m.total)}"
                           + "".join(f"  ·  {label} {hbytes(v)}"
                                     for label, v in core.memory_extra(m))
                           + f"  ·  swap {hbytes(s.swap.used)}", lw=14),
            line("процессы", f"{len(s.procs)} процессов  ·  "
                             f"{sum(p['threads'] for p in s.procs)} потоков", lw=14),
            line("сеть", f"{s.iface or '—'}  ·  шлюз {s.gw or '—'}  ·  DNS {', '.join(s.dns) or '—'}"
                         f"  ·  TCP {s.tcp.get('ESTABLISHED', 0)} соед. / "
                         f"{s.tcp.get('LISTEN', 0)} слушают", lw=14),
        ]
        if s.batt:
            facts = "".join(f"  ·  {k} {v}" for k, v in s.batt_extra)
            rows.append(line("батарея", f"{s.batt.percent:.0f}%{facts}", lw=14))
        self.query_one("#c-sys", Static).update(joined(rows, w))

        versions = sorted({a["version"] for a in s.agents})
        busy = sum(1 for a in s.agents if a["status"] == "busy")
        n, total = s.transcripts
        d = s.today
        crows = [
            line("сессии", f"{len(s.agents)} живых  ·  busy {busy}  ·  "
                           f"{hbytes(sum(a['rss'] for a in s.agents))} RAM  ·  "
                           f"{sum(a['cpu'] for a in s.agents):.1f}% CPU", lw=14),
            line("версии", ", ".join(versions) or "—", lw=14),
            line("сегодня", f"{len(d['sessions'])} сессий  ·  {hnum(d['prompts'])} промптов  ·  "
                            f"{hnum(d['msgs'])} ответов  ·  {hnum(d['tools'])} инструментов  ·  "
                            f"{htok(d['out'])} вых. токенов", lw=14),
            line("транскрипты", f"{n} файлов  ·  {hbytes(total)}  ·  ~/.claude/projects", lw=14),
            line("MCP", "  ·  ".join(s.mcp) or "—", lw=14),
            line("Desktop", Text("запущен", style=GREEN) if s.desktop
                 else Text("не запущен", style=DIM), lw=14),
        ]
        self.query_one("#c-claude", Static).update(joined(crows, w))

        drows = []
        for name, u in s.disks:
            drows.append(line(name[:12], core.bar(u.percent, 28),
                              f"{hbytes(u.used)} / {hbytes(u.total)}   "
                              f"свободно {hbytes(u.free)}   {u.percent:.0f}%", lw=13))
        self.query_one("#c-disks", Static).update(
            joined(drows, w) if drows else Text("—", style=DIM))

        info = s.containers or {"engine": "нет движка", "state": "", "rows": []}
        containers = info["rows"]
        if info["state"]:
            status = Text(info["state"], style=YELLOW)
        else:
            status = Text(f"{len(containers)} контейнер(ов)" if containers else "нет запущенных",
                          style=GREEN if containers else DIM)
        xrows = [line(info["engine"], status, lw=12)]
        for c in containers[:15]:
            xrows.append(line("  " + c[0][:18], c[1], c[2], lw=20))
        self.query_one("#c-cont", Static).update(joined(xrows, w))

    def _draw_temps(self) -> None:
        """Карточка температур: по строке на датчик + самый горячий узел."""
        s = self.s
        w = self._card_width("c-temp", 30)
        card = self.query_one("#c-temp", Static)
        if not s.temps:
            card.update(Text("датчиков температуры нет", style=DIM))
            return
        vw = 7                                   # «73 °C» + отступ
        bw = min(12, w - vw - 12) if w >= 26 else 0
        lw = max(8, w - vw - bw)
        rows = []
        for name, t, crit in s.temps:
            r = Text()
            r.append(f"{clip(name, lw - 1):<{lw}}", style=DIM)
            if bw:
                r.append_text(core.bar(t, bw, lo=101, hi=102,
                                       color=core.temp_color(t, crit)))
            r.append_text(self._fit(Text(f"{t:.0f} °C", style=FG), vw, "right"))
            rows.append(r)
        # итоговые строки живут во всю ширину карточки, а не в колонках выше
        hot = max(s.temps, key=lambda x: x[1])
        h = Text()
        h.append(f"{'ГОРЯЧЕЕ':<9}", style=f"bold {DIM}")
        h.append(f"{clip(hot[0], max(6, w - 17))} {hot[1]:.0f} °C",
                 style=core.temp_color(hot[1], hot[2]))
        rows.append(h)
        g = Text()
        note = "  ~3 мин" if w >= 30 else ""
        g.append(f"{'график':<9}", style=DIM)
        g.append_text(spark(s.hist["temp"], width=max(6, w - 9 - len(note)),
                            scale=100, color=YELLOW))
        g.append(note, style=DIM)
        rows.append(g)
        card.update(joined(rows, w))

    def _gpu_color(self, pct: float) -> str:
        return GREEN if pct < 60 else (YELLOW if pct < 85 else RED)

    def _gpu_head(self, idx: int, name: str, width: int) -> Text:
        """Заголовок блока карты: « GPU 0 · GeForce RTX 5070 Ti ─────── »."""
        t = Text()
        t.append(f" GPU {idx} ", style=f"bold {WHITE} on {PURPLE}")
        t.append("  ")
        t.append(name, style=f"bold {CYAN}")
        pad = width - t.cell_len - 1
        if pad > 0:
            t.append(" " + "─" * pad, style=DIM)
        return t

    def _gpu_metric(self, label: str, pct: float, value: str, unit: str,
                    note, bw: int, lo: float = 60, hi: float = 85) -> Text:
        """Строка метрики: подпись · полоса · число справа · единица · пояснение."""
        t = Text("  ")
        t.append(f"{label:<13}", style=DIM)
        t.append_text(core.bar(pct, bw, lo=lo, hi=hi))
        t.append("  ")
        t.append_text(self._fit(Text(value, style=f"bold {FG}"), 6, "right"))
        t.append(f" {unit:<4}", style=DIM)
        if note is not None:
            t.append("  ")
            t.append_text(note if isinstance(note, Text) else Text(str(note), style=DIM))
        return t

    def _draw_gpu(self) -> None:
        """Карточка на «Обзоре» (компактно) и полная вкладка «Видео»."""
        s = self.s
        card = self.query_one("#c-gpu", Static)
        if not s.gpus:
            card.display = False
            self.query_one("#c-gpumain", Static).update(
                Text("видеокарты NVIDIA не найдены (нет nvidia-smi)", style=DIM))
            self.query_one("#t-gpuproc", DataTable).clear()
            return
        card.display = True

        # ── компактная карточка на обзоре: строка на карту, столбцы через │ ──
        w = self._card_width("c-gpu", 120)
        bw = max(8, min(24, w - 74))
        rows = []
        for c in s.gpus:
            mem = c["used"] * 100 / c["total"] if c["total"] else 0.0
            t = Text(" ")
            t.append(f"GPU {c['idx']}", style=f"bold {PURPLE}")
            t.append("  ")
            t.append_text(core.bar(c["util"], bw))
            t.append_text(self._fit(Text(f"{c['util']:.0f}%",
                                         style=self._gpu_color(c["util"])), 6, "right"))
            cells = [("память", f"{hbytes(c['used'])} / {hbytes(c['total'])}")]
            if w >= 84:
                cells.append(("темп.", f"{c['temp']:.0f} °C"))
            if w >= 100:
                cells.append(("питание", f"{c['power']:.0f} Вт"))
            for label, val in cells:
                t.append("  │ ", style=DIM)
                t.append(f"{label} ", style=DIM)
                t.append(val, style=FG)
            if w >= 118:
                t.append("  │ ", style=DIM)
                t.append(clip(c["name"], 20), style=CYAN)
            rows.append(t)
            hist = Text("        ")
            hist.append_text(spark(s.gpu_hist.get(c["idx"], []),
                                   width=max(20, min(70, w - 18)), scale=100))
            hist.append("  ~7 мин", style=DIM)
            rows.append(hist)
        card.update(joined(rows, w))

        # ── вкладка «Видео»: блок на каждую карту ──
        w = self._card_width("c-gpumain", 120)
        bw = max(10, min(24, w - 62))
        sw = max(20, min(66, w - 30))
        rows = []
        for c in s.gpus:
            mem = c["used"] * 100 / c["total"] if c["total"] else 0.0
            free = c["total"] - c["used"]
            plim = c["plimit"] or 1
            cmax = c["clock_max"] or 1
            rows.append(self._gpu_head(c["idx"], c["name"], w))
            rows.append(self._gpu_metric(
                "загрузка", c["util"], f"{c['util']:.0f}", "%",
                Text("как долго карта была занята", style=DIM), bw))
            rows.append(self._gpu_metric(
                "видеопамять", mem, f"{mem:.0f}", "%",
                Text(f"{hbytes(c['used'])} из {hbytes(c['total'])}  ·  "
                     f"свободно {hbytes(free)}", style=DIM), bw, lo=75, hi=92))
            rows.append(self._gpu_metric(
                "температура", c["temp"], f"{c['temp']:.0f}", "°C",
                Text(f"вентилятор {c['fan']:.0f} %" if c["fan"] else "вентилятор —",
                     style=DIM), bw, lo=70, hi=84))
            rows.append(self._gpu_metric(
                "питание", c["power"] * 100 / plim, f"{c['power']:.0f}", "Вт",
                Text(f"из {plim:.0f} Вт лимита  ·  {c['power'] * 100 / plim:.0f} %",
                     style=DIM), bw, lo=70, hi=90))
            rows.append(self._gpu_metric(
                "частота", c["clock"] * 100 / cmax, f"{c['clock']:.0f}", "МГц",
                Text(f"из {cmax:.0f} МГц максимума", style=DIM), bw, lo=101, hi=102))
            rows.append(Text(""))
            for label, hist, color in (("↗ загрузка", s.gpu_hist, None),
                                       ("↗ память", s.gpu_mem_hist, PURPLE)):
                g = Text("  ")
                g.append(f"{label:<13}", style=DIM)
                g.append_text(spark(hist.get(c["idx"], []), width=sw,
                                    scale=100, color=color))
                g.append("  0-100 % за ~7 мин" if w >= 112 else "  ~7 мин", style=DIM)
                rows.append(g)
            rows.append(Text(""))

        used = sum(c["used"] for c in s.gpus)
        total = sum(c["total"] for c in s.gpus)
        power = sum(c["power"] for c in s.gpus)
        peak = max(c["util"] for c in s.gpus)
        tot = Text("  ")
        tot.append(f"{'ИТОГО':<13}", style=f"bold {DIM}")
        cells = (("карт", str(len(s.gpus)), FG),
                 ("пик загрузки", f"{peak:.0f} %", self._gpu_color(peak)),
                 ("видеопамять", f"{hbytes(used)} / {hbytes(total)}", FG),
                 ("питание", f"{power:.0f} Вт", FG))
        for i, (label, val, style) in enumerate(cells):
            if i:
                tot.append("   │   ", style=DIM)
            tot.append(f"{label} ", style=DIM)
            tot.append(val, style=style)
        rows.append(tot)
        self.query_one("#c-gpumain", Static).update(joined(rows, w))

        # ── таблица процессов на картах ──
        t = self.query_one("#t-gpuproc", DataTable)
        t.clear()
        total_by_gpu = {c["idx"]: c["total"] for c in s.gpus}
        for p in s.gpu_procs[:20]:
            cap = total_by_gpu.get(p["gpu"], 0)
            share = p["mem"] * 100 / cap if cap else 0.0
            t.add_row(Text(f"GPU {p['gpu']}", style=PURPLE), str(p["pid"]),
                      Text(clip(p["name"], 28), style=FG), hbytes(p["mem"]),
                      core.bar(share, 10), Text(f"{share:.0f} %",
                                                style=self._gpu_color(share)))

    def _draw_net_tab(self) -> None:
        s = self.s
        addrs = psutil.net_if_addrs()
        ip = next((a.address for a in addrs.get(s.iface, [])
                   if a.family.name == "AF_INET"), "—")
        w = self._card_width("c-netmain", 120)
        sw = max(20, min(80, w - 14))
        vpn = []
        for name, lst in addrs.items():
            if not name.startswith(core.VPN_PREFIXES):
                continue
            for a in lst:
                if a.family.name == "AF_INET":
                    tag = ("vpn" if a.address.startswith(("10.8.", "10.9."))
                           else "tailscale" if a.address.startswith("100.")
                           else "туннель")
                    vpn.append(f"{name} {a.address} ({tag})")
        rows = [
            line("маршрут", f"{s.iface}  →  {s.gw}", lw=12),
            line("адрес", ip, f"DNS {', '.join(s.dns) or '—'}", lw=12),
            line("VPN", "  ·  ".join(vpn) or "туннелей нет", lw=12),
            line("TCP", f"{s.tcp.get('ESTABLISHED', 0)} активных  ·  "
                        f"{s.tcp.get('LISTEN', 0)} слушают  ·  "
                        f"{s.tcp.get('TIME_WAIT', 0)} time-wait  ·  "
                        f"{s.tcp.get('CLOSE_WAIT', 0)} close-wait", lw=12),
            Text(""),
            line("приём", hrate(s.net_rate[0]), lw=12),
            spark(s.hist["rx"], width=sw, color=GREEN),
            line("отдача", hrate(s.net_rate[1]), lw=12),
            spark(s.hist["tx"], width=sw, color=BLUE),
        ]
        self.query_one("#c-netmain", Static).update(joined(rows, w))

        t = self.query_one("#t-net", DataTable)
        t.clear()
        for name, (rx, tx) in sorted(s.net_all.items()):
            if rx == 0 and tx == 0:
                continue
            addr = next((a.address for a in addrs.get(name, [])
                         if a.family.name == "AF_INET"), "—")
            if name.startswith(core.VPN_PREFIXES):
                kind = ("vpn" if addr.startswith(("10.8.", "10.9."))
                        else "tailscale" if addr.startswith("100.")
                        else "туннель")
            elif name == core.OS.LOOPBACK:
                kind = "loopback"
            elif name == s.iface:
                kind = "основной"
            else:
                kind = "—"
            r = s.if_rates.get(name, (0.0, 0.0))
            t.add_row(Text(name, style=CYAN if name == s.iface else FG), addr,
                      Text(hrate(r[0]), style=GREEN if r[0] > 1024 else DIM),
                      Text(hrate(r[1]), style=BLUE if r[1] > 1024 else DIM),
                      hbytes(rx), hbytes(tx), Text(kind, style=DIM))

        pt = self.query_one("#t-ports", DataTable)
        pt.clear()
        known = {8787: "assistant", 5000: "AirPlay", 7000: "AirPlay", 21118: "RustDesk",
                 22: "ssh", 80: "http", 443: "https", 5432: "postgres", 6379: "redis",
                 3000: "dev-сервер", 8080: "http-alt", 11434: "ollama"}
        for port, host, name, pid, hint in s.ports:
            local = host in ("127.0.0.1", "::1", "localhost")
            label = known.get(port) or hint or ("локально" if local else "снаружи")
            exposed = not local and port not in known
            pt.add_row(Text(str(port), style=CYAN), Text(host, style=FG),
                       Text(name, style=FG), str(pid),
                       Text(label + ("" if local else "  ·  снаружи"),
                            style=YELLOW if exposed else (FG if hint else DIM)))
        pt.border_subtitle = f"{len(s.ports)} слушающих сокетов · {core.OS.PORTS_SOURCE}"


def main() -> int:
    MxStat().run()
    return 0
