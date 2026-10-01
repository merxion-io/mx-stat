"""Merxion Stat · выбор платформенного слоя — единственное место, где смотрим на ОС.

Всё, что зависит от операционной системы, живёт в platform_linux.py и
platform_macos.py с одинаковым интерфейсом; core и tui работают с `OS`:

  NAME, VPN_PREFIXES, LOOPBACK, PORTS_SOURCE, TEMP_NOTE,
  POWER_NOTE / POWER_NOTE_DENIED      — подписи (снапшот, широкая, узкая карточка)
  host_name, cpu_brand, os_label, kernel_short, machine_model
  cpu_groups(n) -> [(группа, префикс ядра, [индексы])]
  cpu_summary(short) -> «4P + 4E ядер» / «8 ядер / 16 потоков»
  memory_extra(mem) -> [(подпись, байт)]
  disk_name(partition) -> имя тома или None (не показывать)
  battery_facts() -> [(подпись, значение)]
  default_iface, net_counters, dns_servers, wifi_ssid
  listening_sockets() -> [(порт, адрес, pid)], tcp_summary()
  containers() -> {engine, state, rows} | None
  temperatures() -> ([процессор], [прочее]) строк (узел, °C, критическая)
  extra_power() -> [(узел, ватты, в сумму)], PowerMeter (CPU)
"""
import sys

if sys.platform == "darwin":
    import platform_macos as OS
elif sys.platform.startswith("linux"):
    import platform_linux as OS
else:
    raise SystemExit(f"Merxion Stat: платформа {sys.platform} не поддерживается "
                     "(есть Linux и macOS)")

__all__ = ["OS"]
