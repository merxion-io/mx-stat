#!/usr/bin/env bash
# Установка Merxion Stat (MERXION) на этот компьютер.
#
#   ./install.sh                → ~/.local/share/mx-stat + лаунчер ~/.local/bin/mx
#   MXSTAT_HOME=… MXSTAT_BIN=…  → другие пути
#
# Работает на Linux и macOS (bash 3.2, BSD-утилиты). Нужен Python 3.11+
# (venv создаётся через uv, если он есть, иначе python3 -m venv).
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${MXSTAT_HOME:-$HOME/.local/share/mx-stat}"
BIN="${MXSTAT_BIN:-$HOME/.local/bin}"
PYMIN="3.11"

say()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$SRC/share/tui.py" ] || die "рядом с install.sh нет папки share/ — запускайте из распакованного архива"

say "Каталог программы: $DEST"
mkdir -p "$DEST" "$BIN"
cp "$SRC"/share/*.py "$DEST"/
cp "$SRC"/requirements.txt "$DEST"/

# ── виртуальное окружение ──
if [ ! -x "$DEST/venv/bin/python" ]; then
    if command -v uv >/dev/null 2>&1; then
        say "Создаю venv через uv (Python >= $PYMIN)"
        uv venv --quiet --python ">=$PYMIN" "$DEST/venv"
    else
        PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
        [ -n "$PY" ] || die "не найден python3 (нужен $PYMIN+); поставьте его или uv: https://docs.astral.sh/uv/"
        "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" \
            || die "$PY слишком старый, нужен Python $PYMIN+"
        say "Создаю venv через $PY"
        "$PY" -m venv "$DEST/venv"
    fi
fi

say "Ставлю зависимости (textual, psutil, rich)"
if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$DEST/venv/bin/python" -r "$DEST/requirements.txt"
else
    "$DEST/venv/bin/python" -m pip install --quiet --upgrade pip
    "$DEST/venv/bin/python" -m pip install --quiet -r "$DEST/requirements.txt"
fi

# ── лаунчер ──
# Тело держим синхронным с переносимым вариантом bin/mx.
cat > "$BIN/mx" <<EOF
#!/bin/sh
case "\${1:-}" in
    stat) ;;
    -h|--help|"") echo "mx stat [-s] [-w [сек]] [--no-color]   — монитор системы и агентов"; exit 0 ;;
    *) echo "mx: неизвестная команда '\$1' (доступно: stat)" >&2; exit 2 ;;
esac
exec "$DEST/venv/bin/python" "$DEST/mxstat.py" "\$@"
EOF
chmod +x "$BIN/mx"
say "Лаунчер: $BIN/mx"

# ── проверка ──
"$BIN/mx" stat -h >/dev/null || die "лаунчер не запускается"
"$DEST/venv/bin/python" -c "import textual, psutil, rich; print('   textual', textual.__version__, '· psutil', psutil.__version__)"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) printf '\033[1;33m!\033[0m %s\n' "$BIN не в PATH — добавьте в ~/.zshrc:  export PATH=\"$BIN:\$PATH\"" ;;
esac

say "Готово. Запуск:  mx stat   (снимок: mx stat -s)"
