# mx-stat — system & AI agent monitor

[Русская версия](README.ru.md) · MIT · Linux · Python 3.11+

A full-screen terminal monitor (Textual, tokyo-night theme). Beyond the usual `htop` view
it shows **live coding-agent sessions**: what each one is working on, which model, how much
context is consumed, when it last wrote, a per-minute response pulse and an hourly histogram.

Agents are discovered per provider; how much detail is available depends on what each one
writes to disk:

| Provider | Detected | Task, model, context, tokens |
|---|---|---|
| Claude Code | ✅ | ✅ full, plus branch, permission mode and daily token stats |
| Codex | ✅ | ✅ full, plus daily token stats and real busy/idle from turn events |
| opencode | ✅ | ✅ task, model, session tokens (no context window, no daily breakdown) |
| muse, crush, qwen, aider, gemini | ✅ process only | — no on-disk session state to read |

Adding a provider is one function in `share/core.py` — see `_codex_agents` /
`_opencode_agents` for the shape.

```
█▀▄▀█ █▀▀ █▀█ ▀▄▀ █ █▀█ █▄ █  │ AMD Ryzen 9 · Fedora 42 · ↑ 9d 21h · Tue 08.09  11:42:21
█ ▀ █ ██▄ █▀▄ ▄▀▄ █ █▄█ █ ▀█  │ CPU 24.3%  RAM 68%  load 2.49  ↓ 10 KB/s  ↑ 220 KB/s  🤖 5 busy 3 · 1s  ·  Stat
```

> **Note:** the interface is currently in Russian. Documentation is bilingual; UI
> localisation is not done yet — contributions welcome.

## Install

```sh
git clone https://github.com/merxion-io/mx-stat.git && cd mx-stat
./install.sh
mx stat            # TUI
mx stat -s         # one static snapshot (good for pipes / screenshots)
```

`install.sh` copies the code to `~/.local/share/mx-stat`, creates an isolated venv there
(via `uv` if available, otherwise `python3 -m venv`) and writes a launcher at
`~/.local/bin/mx`. Requires Python 3.11+. Paths are overridable via `MXSTAT_HOME` and
`MXSTAT_BIN`.

`mx` is a thin wrapper — right now it knows a single subcommand, `stat`.

Headless check (clicks through every tab, no terminal needed):

```sh
~/.local/share/mx-stat/venv/bin/python tests/smoke_test.py 140 46 --show
```

## What's inside

| Tab | Contents |
|---|---|
| **1 Overview** | CPU as a table (cores as columns, USER/SYS, per-core history, P/E clusters, threads, syscalls, top processes), memory with top RAM consumers, power/battery, agent table, today's tokens with a pulse, network, disk |
| **2 Agents** | Wide session table (branch, permission mode, cli/sdk type, prompts, tools, tokens), details for the selected session (task, last request, effort, last tool, cache hit), hourly output-token histogram, 7 days |
| **3 Processes** | Top 60 processes; `s` cycles sorting CPU → memory → threads → age; `f` filters to claude only |
| **4 System** | Machine, kernel, load, memory, network, battery; Claude Code card (versions, transcripts, MCP); volumes; podman/docker containers |
| **5 Network** | Route, DNS, VPN tunnels, TCP by state, per-interface throughput, listening ports with owning process |
| **1 Overview** — ⚡ POWER card | Live draw per node: CPU (RAPL counters), each GPU (nvidia-smi), integrated graphics (hwmon), total and a ~3 minute graph. Disks, RAM, board and PSU are not measured — no sensors exist |
| **1 Overview** — 🌡 TEMPERATURE card | Every sensor: CPU and its dies (k10temp), each GPU, integrated graphics, network chip; hottest node and a ~3 minute graph. Disk temperatures would need smartctl — not used |
| **6 GPU** | NVIDIA cards: utilisation and VRAM with history, temperature, fan, power and clocks; table of processes holding VRAM |

Keys: `1`–`6` tabs · `p` pause · `s` sort · `f` claude filter · `r` refresh ·
`+`/`-` interval (0.5–10 s) · `q` quit.

## Where the agent data comes from

Everything is read from each agent's own files — no API, no network.

### Claude Code

- **`~/.claude/sessions/<pid>.json`** — live sessions: `sessionId`, `cwd`, `name`, `status`
  (`idle`/`busy`/`shell`), `version`, `startedAt`, `entrypoint` (`cli` / `sdk-cli`).
  The process is verified through psutil: Claude Code's process name is its version number
  (`2.1.263`), so the reliable signal is an `exe` path under `~/.local/share/claude/versions/`.
- **Transcript** `~/.claude/projects/<cwd with / and . → ->/<sessionId>.jsonl`:
  - `{"type":"ai-title","aiTitle":…}` — task name (TASK column);
  - `{"type":"last-prompt","lastPrompt":…}` — the user's last request;
  - `"role":"assistant"` lines carrying `"usage":` — tokens, model, `effort`; context =
    `input + cache_read + cache_creation` of the last response; an `[1m]` marker at the top
    of the file means a 1M limit, otherwise 200K;
  - `gitBranch`, `permissionMode`, `entrypoint` — on user/assistant lines;
  - a human prompt is `"type":"user"` plus `"origin":{"kind":"human"}`.
  Transcripts are read **incrementally** (`SessionTracker` in `core.py`) from the last
  offset, so multi-megabyte sessions cost nothing. Timestamps in the files are UTC; hourly
  and per-minute buckets are converted to local time.
- Today's and the 7-day statistics come from a regex scan of every `*.jsonl` with an mtime
  within the last 36 h / 8 days, refreshed every 20 s on a background thread.
  Codex rollouts are scanned the same way and added in, so the daily cards and the 7-day
  histogram cover both providers; the model breakdown names each one. **opencode is not
  included** — its database stores one running total per session with no per-day breakdown,
  so its tokens cannot honestly be attributed to a date.

### Codex

`~/.codex/sessions/<year>/<month>/<day>/rollout-*.jsonl` — one append-only transcript per
session, read incrementally exactly like Claude's. `turn_context` carries the model and
reasoning effort, `token_count` the live context plus `model_context_window`, and
`task_started` / `task_complete` give a genuine busy/idle status. Thread names come from
`~/.codex/session_index.jsonl`. A live process is tied to its session by the UUID in
`codex resume <uuid>`, or failing that by the newest transcript in the same working
directory that was written to during the process's lifetime.

Rollouts reach tens of megabytes, so the first pass counts responses with a byte scan and
JSON-parses only the last 4 MB: everything displayed is rewritten on every turn, so the
history is not needed.

### opencode

`~/.local/share/opencode/opencode.db`, the `session` table only — title, model, directory
and per-session token counters. That table holds dozens of rows even when the database is
hundreds of megabytes, and it is opened read-only. opencode records no context-window size,
so the accumulated input is shown in place of a context bar.

## Where the system data comes from (Linux)

Read straight from the kernel; no daemons required:

- **CPU and model** — `/proc/cpuinfo`; distribution — `/etc/os-release`.
- **Power** — RAPL counters in `/sys/class/powercap`: watts are the microjoule delta between
  polls. Disks, RAM, board and PSU are not measured — no sensors.
- **Temperatures** — `/sys/class/hwmon` (CPU via k10temp), GPUs via `nvidia-smi`.
- **Battery** — cycles and health from `/sys/class/power_supply`, when the kernel exposes them.
- **Network** — default route via `ip -4 route show default`, DNS from `/etc/resolv.conf`,
  Wi-Fi via `nmcli`, TCP sockets and listening ports via `psutil`.
- **Containers** — `podman ps`, falling back to `docker ps`; with neither installed the card
  is simply hidden.
- **GPUs** — `nvidia-smi` (utilisation, VRAM, temperature, clocks, processes). Without an
  NVIDIA card tab 6 is empty.

## Files

```
share/core.py       data collection, SessionTracker, logo, static rendering (rich)
share/tui.py        Textual app: 6 tabs, adaptive columns, live updates
share/snapshot.py   the mx stat -s / -w mode
share/mxstat.py     entry point (the launcher invokes it through the venv)
bin/mx              example shell launcher (install.sh writes its own with absolute paths)
install.sh          installation
requirements.txt    textual==8.2.8 · psutil==7.2.2 · rich==15.0.0
tests/smoke_test.py headless check via Textual Pilot
CLAUDE-INSTALL.md   instructions for Claude Code on a fresh machine
```

Logo and palette are the `LOGO`, `BRAND`, `WHITE` constants at the top of `core.py`.

## License

MIT — see [LICENSE](LICENSE).

© 2026 Merxion Solution S.R.L.
