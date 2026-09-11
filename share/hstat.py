"""hstat — live-монитор системы и AI-агентов Claude Code (MERXION).

  hstat              полноэкранный TUI (по умолчанию)
  hstat -s           один статичный снимок (годится для пайпов)
  hstat -s -w [сек]  простой перерисовывающийся вывод без TUI
  hstat --no-color   снимок без цвета

Клавиши в TUI: 1-6 вкладки (6 — видеокарты) · p пауза · s сортировка процессов · f только claude
               r пересчитать · +/- интервал · q выход
"""
import os
import sys

# модули лежат рядом с этим файлом — не зависим от того, куда установлен пакет
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

args = sys.argv[1:]
if "-h" in args or "--help" in args:
    print(__doc__.strip())
    sys.exit(0)

if {"-s", "--snapshot", "-w", "--watch", "--no-color"} & set(args):
    import snapshot
    sys.exit(snapshot.main())

import tui
sys.exit(tui.main())
