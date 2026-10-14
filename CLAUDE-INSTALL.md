# Instructions for Claude Code: install mx-stat on this machine

You have the `mx-stat` repository — a finished tool, nothing needs to be written from scratch.
Your job: install it, verify it, and tell the user how to run it.

## Steps

1. **Install**
   ```sh
   git clone https://github.com/merxion-io/mx-stat.git && cd mx-stat
   ./install.sh
   ```
   The script copies the code to `~/.local/share/mx-stat`, creates a venv (uv, else
   `python3 -m venv`), installs `textual`, `psutil`, `rich` from `requirements.txt` and
   writes a launcher at `~/.local/bin/mx`. Requires Python 3.11+; if it is missing but `uv`
   is present, uv will fetch a suitable Python itself.

2. **Check PATH.** If `install.sh` warned that `~/.local/bin` is not on PATH:
   ```sh
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   ```
   (or `~/.bashrc` for bash). Do not add the line if an equivalent one already exists.

3. **Verify without a terminal** (never launch the TUI from a non-interactive Bash — it hangs):
   ```sh
   mx stat -h
   COLUMNS=120 mx stat -s --no-color | head -40
   ~/.local/share/mx-stat/venv/bin/python tests/smoke_test.py 140 46
   ```
   The first prints help, the second a static snapshot with every section, the third clicks
   through all six tabs headless and prints `smoke test OK`. If all three finish without a
   traceback, the installation works.

4. **Tell the user**: run `mx stat` in a terminal (140+ columns is better; at 200+ the agent
   table shows every column), `mx stat -s` for a snapshot. Keys are listed in `mx stat -h`.

## What not to do

- Do not run `mx stat` without flags from your own Bash — it is a full-screen TUI and needs a TTY.
- Do not rewrite the code "to taste" before it has run as-is. Run `install.sh` and verify first,
  then make changes if the user asks.
- Do not install dependencies into the system Python — only into the venv `install.sh` creates.

## If something goes wrong

| Symptom | What to do |
|---|---|
| `install.sh: python3 not found` | Install `uv` (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) and retry: uv downloads Python itself |
| `mx stat -s` works but shows 0 agents | Normal when no Claude Code sessions are running. Agents come from `~/.claude/sessions/*.json` |
| Agents show `…` instead of context for the first few seconds | Normal: the transcript tracker is warming up in the background |
| Everything works but the CONTAINERS card is empty | Neither `podman` nor `docker` is installed. That card is optional |
| Empty POWER / TEMPERATURE cards, or an empty GPU tab | The corresponding sensors are absent: RAPL in `/sys/class/powercap`, hwmon, `nvidia-smi`. Not a bug |
| macOS: only the battery in TEMPERATURE, only the whole machine in POWER | Expected: SoC sensors need root (`powermetrics`); without root the battery controller is the only source |
| Neither Linux nor macOS | Not supported: the platform layer exists for Linux (`share/platform_linux.py`) and macOS (`share/platform_macos.py`) only |

## Layout after installation

```
~/.local/bin/mx                    launcher (sh → venv python → mxstat.py)
~/.local/share/mx-stat/mxstat.py   entry point
~/.local/share/mx-stat/core.py     agents + SessionTracker + logo (LOGO/BRAND/WHITE)
~/.local/share/mx-stat/platform_*.py  system data per OS (picked in platforms.py)
~/.local/share/mx-stat/tui.py      Textual application
~/.local/share/mx-stat/snapshot.py the -s mode
~/.local/share/mx-stat/venv/       environment
```

See `README.md` for details on the data sources.
