# SPDX-License-Identifier: AGPL-3.0-only
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
  battery_detail() -> dict | None     — живая батарея для вкладки 7 (None — батареи нет):
      source 'adapter'|'battery', status 'charging'|'discharging'|'full'|'idle',
      percent, voltage В, current А (− разряд), power Вт (− разряд),
      time_empty / time_full мин, cells [В], cells_ra, qmax, unit + cap_now/full/
      nominal/design, cycles, cycles_design, temp °C, lifetime {}, charger {},
      updated epoch | None, live {sys, in, volt, amp} | None (посекундно), adapter {watts, voltage, current, name, maker, wireless, profiles, active} | None,
      flow {in, load, batt, loss, wall, in_v, in_a} Вт/В/А | None, device, src_note;
      чего платформа не знает — None или пусто
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
