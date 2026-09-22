# Инструкция для Claude Code: поставить Merxion Stat на эту машину

Ты получил архив `MerxionStat-*.zip` — готовый инструмент, ничего писать с нуля не нужно.
Задача: установить, проверить, показать пользователю, как запускать.

## Шаги

1. **Распаковать и установить**
   ```sh
   cd ~/Downloads && unzip -o MerxionStat-*.zip && cd MerxionStat
   ./install.sh
   ```
   Скрипт копирует код в `~/.local/share/MerxionStat`, создаёт venv (uv → иначе `python3 -m venv`),
   ставит `textual`, `psutil`, `rich` из `requirements.txt` и пишет лаунчер `~/.local/bin/mx`.
   Нужен Python 3.11+. Если его нет, а есть `uv` — он скачает нужный Python сам.
   Если на машине осталась прошлая версия под именем `hstat`, install.sh сам удалит
   `~/.local/bin/hstat` и `~/.local/share/hstat`.

2. **Проверить PATH.** Если `install.sh` предупредил, что `~/.local/bin` не в PATH:
   ```sh
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   ```
   (или `~/.bashrc`, если оболочка bash). Новую строку не добавляй, если такая уже есть.

3. **Проверить без терминала** (TUI нельзя запускать из неинтерактивного Bash — он зависнет):
   ```sh
   mx stat -h
   COLUMNS=120 mx stat -s --no-color | head -40
   ~/.local/share/MerxionStat/venv/bin/python tests/smoke_test.py 140 46
   ```
   Первая команда печатает справку, вторая — статичный снимок со всеми секциями, третья
   прощёлкивает все шесть вкладок headless и печатает `smoke test OK`. Если все три отработали
   без traceback — установка рабочая.

4. **Сказать пользователю**: запускать `mx stat` в терминале (лучше шириной 140+ колонок,
   на 200+ таблица агентов показывает все колонки), `mx stat -s` для снимка. Клавиши перечислены
   в `mx stat -h`.

## Что не делать

- Не запускай `mx stat` без флагов из своего Bash — это полноэкранный TUI, ему нужен TTY.
- Не переписывай код «под себя» до того, как он запустился как есть. Сначала `install.sh`
  и проверка, потом правки по просьбе пользователя.
- Не ставь зависимости в системный Python — только venv, который создаёт `install.sh`.

## Если что-то пошло не так

| Симптом | Что делать |
|---|---|
| `install.sh: не найден python3` | Поставить `uv` (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) и повторить: uv скачает Python сам |
| `mx stat -s` работает, агентов 0 | Нормально, если сейчас нет запущенных сессий Claude Code. Агенты появляются из `~/.claude/sessions/*.json` |
| В TUI у агентов `…` вместо контекста первые секунды | Нормально: трекер транскриптов прогревается в фоне |
| Всё на месте, но карточка КОНТЕЙНЕРЫ пустая | Нет ни `podman`, ни `docker`. Это опционально |
| Пустые карточки ЭНЕРГИЯ / ТЕМПЕРАТУРА / вкладка «Видео» | Нет соответствующих датчиков: RAPL в `/sys/class/powercap`, hwmon, `nvidia-smi`. Не баг |
| Не Linux | Это Linux-версия: mac-код вырезан, данные берутся из `/proc`, `/sys`, `/etc`. На других системах не запустится осмысленно |

## Где что лежит после установки

```
~/.local/bin/mx                       лаунчер (sh → venv python → mxstat.py)
~/.local/share/MerxionStat/mxstat.py  точка входа
~/.local/share/MerxionStat/core.py    данные + SessionTracker + логотип (LOGO/BRAND/WHITE)
~/.local/share/MerxionStat/tui.py     Textual-приложение
~/.local/share/MerxionStat/snapshot.py режим -s
~/.local/share/MerxionStat/venv/      окружение
```

Подробности про источники данных — в `README.md` рядом.
