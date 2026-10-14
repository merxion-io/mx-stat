# SPDX-License-Identifier: AGPL-3.0-only
"""Merxion Stat · snapshot — одноразовый статичный вывод (mx stat -s) и простой watch."""
import os
import sys
import time

from rich.console import Console

from core import build, emit


def main() -> int:
    args = [a for a in sys.argv[1:] if a not in ("-s", "--snapshot")]
    no_color = "--no-color" in args or os.environ.get("NO_COLOR")
    width = min(int(os.environ.get("COLUMNS", 0) or 0) or 118, 132)
    console = Console(color_system=None if no_color else "truecolor",
                      width=width, highlight=False, soft_wrap=False)
    if "-w" in args or "--watch" in args:
        i = args.index("-w" if "-w" in args else "--watch")
        nxt = args[i + 1] if len(args) > i + 1 else ""
        interval = float(nxt) if nxt.replace(".", "", 1).isdigit() else 2.0
        try:
            console.print("\033[?25l", end="")
            while True:
                out = build(console)
                console.print("\033[H\033[2J", end="")
                emit(console, out)
                time.sleep(max(0.0, interval - 0.6))
        except KeyboardInterrupt:
            pass
        finally:
            console.print("\033[?25h", end="")
        return 0
    emit(console, build(console))
    return 0
