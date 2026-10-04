"""Headless-проверка Merxion Stat: запускает TUI без терминала, прощёлкивает вкладки и клавиши.

    ~/.local/share/mx-stat/venv/bin/python tests/smoke_test.py [ширина] [высота] [--show]

Без --show печатает только итог; с --show выводит текстовый слепок каждой вкладки.
Код выхода 0 — всё отрисовалось без исключений."""
import asyncio
import html
import os
import re
import sys

MXSTAT_HOME = os.environ.get("MXSTAT_HOME", os.path.expanduser("~/.local/share/mx-stat"))
sys.path.insert(0, MXSTAT_HOME)

import tui  # noqa: E402


def svg_to_text(svg: str) -> str:
    """Слепок экрана из SVG-скриншота Textual: группируем <text> по координате y."""
    rows: dict[int, list] = {}
    for m in re.finditer(r'<text[^>]*?x="([\d.]+)"[^>]*?y="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        x, y, body = float(m.group(1)), float(m.group(2)), m.group(3)
        body = html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ")
        rows.setdefault(round(y), []).append((x, body))
    out = []
    for y in sorted(rows):
        line = ""
        for x, body in sorted(rows[y]):
            col = int(round(x / 12.2))
            line += " " * max(0, col - len(line)) + body
        out.append(line.rstrip())
    return "\n".join(out)


async def main(width: int, height: int, show: bool) -> int:
    app = tui.MxStat()
    async with app.run_test(size=(width, height)) as pilot:
        await asyncio.sleep(4.0)
        for tab in ("1", "2", "3", "4", "5", "6", "7"):
            await pilot.press(tab)
            await asyncio.sleep(1.2)
            if show:
                print(f"\n{'=' * 28} вкладка {tab} ({width}x{height}) {'=' * 28}")
                print(svg_to_text(app.export_screenshot()))
        for key in ("s", "f", "r", "p", "p", "plus", "minus"):
            await pilot.press(key)
            await asyncio.sleep(0.4)
        await pilot.press("q")
    print(f"smoke test OK ({width}x{height}) · агентов найдено: {len(app.s.agents)}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    w = int(args[0]) if args else 140
    h = int(args[1]) if len(args) > 1 else 46
    sys.exit(asyncio.run(main(w, h, "--show" in sys.argv)))
