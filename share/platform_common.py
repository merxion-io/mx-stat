"""Merxion Stat · общие мелочи платформенного слоя (без rich и без core)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def sh(cmd: list[str] | str, timeout: float = 3.0) -> str:
    """Тихо выполнить команду, вернуть stdout ('' при любой ошибке)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def which(cli: str) -> bool:
    """Есть ли команда в PATH (без запуска)."""
    return any(Path(p, cli).exists() for p in os.environ.get("PATH", "").split(":") if p)


def ps_rows(cli: str) -> list[list[str]]:
    """Запущенные контейнеры: [[имя, статус, образ]] из `<cli> ps`."""
    out = sh([cli, "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"], timeout=8)
    return [ln.split("\t") for ln in out.splitlines() if ln.count("\t") == 2]


class NoPower:
    """Замерщик-заглушка: платформа не умеет мерить CPU отдельно."""

    denied = False
    denied_note = ""

    def sample(self) -> None:
        pass

    def cpu_rows(self) -> list[tuple[str, float, bool]]:
        return []
