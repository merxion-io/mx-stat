"""Проверка разбора батареи без железа: фикстуры AppleSmartBattery (macOS) и sysfs (Linux).

    ~/.local/share/mx-stat/venv/bin/python tests/battery_test.py

Код выхода 0 — все проверки прошли. Linux-ветка гоняется на временной папке и на macOS."""
import os
import sys
import tempfile
from pathlib import Path

MXSTAT_HOME = os.environ.get("MXSTAT_HOME", os.path.expanduser("~/.local/share/mx-stat"))
sys.path.insert(0, MXSTAT_HOME)

import platform_linux  # noqa: E402
import platform_macos  # noqa: E402

# срез настоящего MacBook (bq20z451, 3 банки) без блока питания
MAC_BATTERY = {
    "Voltage": 11034, "InstantAmperage": (1 << 64) - 2024, "CurrentCapacity": 61,
    "AppleRawCurrentCapacity": 2616, "AppleRawMaxCapacity": 4477, "DesignCapacity": 5103,
    "NominalChargeCapacity": 4610, "CycleCount": 718, "DesignCycleCount9C": 1000,
    "Temperature": 3056, "IsCharging": False, "ExternalConnected": False, "FullyCharged": False,
    "TimeRemaining": 179, "AvgTimeToFull": 65535, "DeviceName": "bq20z451",
    "AdapterDetails": {"FamilyCode": 0},
    "BatteryData": {"CellVoltage": [3662, 3702, 3671], "WeightedRa": [155, 134, 151],
                    "Qmax": [5070, 5085, 5079],
                    "LifetimeData": {"MinimumPackVoltage": 8518, "MaximumPackVoltage": 13053,
                                     "MaximumChargeCurrent": 4354, "MaximumDischargeCurrent": -3188,
                                     "MinimumTemperature": 48, "MaximumTemperature": 455,
                                     "AverageTemperature": 235}},
    "ChargerData": {"ChargingVoltage": 4288, "ChargingCurrent": 0, "NotChargingReason": 128},
    "PowerTelemetryData": {"SystemPowerIn": 0, "SystemLoad": 18392, "BatteryPower": -18392,
                           "AdapterEfficiencyLoss": 0},
}
MAC_ADAPTER = {
    "AdapterVoltage": 20000, "Current": 3000, "Name": "61W USB-C Power Adapter",
    "Manufacturer": "Apple Inc.", "Watts": 61, "IsWireless": False, "UsbHvcHvcIndex": 1,
    "UsbHvcMenu": [{"MaxVoltage": 5000, "MaxCurrent": 3000}, {"MaxVoltage": 20000, "MaxCurrent": 3000}],
}

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


def mac() -> None:
    print("macOS · от батареи")
    d = platform_macos.parse_battery(MAC_BATTERY)
    check("ток знаковый: 2^64−2024 → −2.024 А", d["current"] == -2.024)
    check("напряжение 11.034 В", d["voltage"] == 11.034)
    check("3 банки", d["cells"] == [3.662, 3.702, 3.671])
    check("разряд и время до разряда", d["status"] == "discharging" and d["time_empty"] == 179)
    check("65535 = времени нет", d["time_full"] is None)
    check("блока нет", d["adapter"] is None and d["source"] == "battery")
    check("система 18.392 Вт, батарея −18.392 Вт", d["flow"]["load"] == 18.392 and d["power"] == -18.392)
    check("температура 30.56 °C, рекорд 45.5 °C",
          d["temp"] == 30.56 and d["lifetime"]["t_max"] == 45.5)

    print("macOS · на блоке, заряд")
    d = platform_macos.parse_battery({
        **MAC_BATTERY, "ExternalConnected": True, "IsCharging": True, "InstantAmperage": 2100,
        "AvgTimeToFull": 85, "AdapterDetails": MAC_ADAPTER,
        "PowerTelemetryData": {"SystemPowerIn": 45200, "SystemLoad": 18400, "BatteryPower": 24100,
                               "AdapterEfficiencyLoss": 2700, "SystemVoltageIn": 20010,
                               "SystemCurrentIn": 2259}})
    check("заряд, до полного 85 мин", d["status"] == "charging" and d["time_full"] == 85)
    check("блок 61 Вт, 20 В × 3 А", d["adapter"]["watts"] == 61 and d["adapter"]["voltage"] == 20.0)
    check("профили PD и активный", d["adapter"]["profiles"] == [(5.0, 3.0), (20.0, 3.0)]
          and d["adapter"]["active"] == 1)
    check("поток: от блока 45.2, система 18.4, в батарею +24.1, потери 2.7",
          (d["flow"]["in"], d["flow"]["load"], d["flow"]["batt"], d["flow"]["loss"])
          == (45.2, 18.4, 24.1, 2.7))

    print("macOS · скудный набор ключей (другая модель)")
    d = platform_macos.parse_battery({"Voltage": 12000, "CurrentCapacity": 50,
                                      "InstantAmperage": (1 << 64) - 527})
    check("без BatteryData/ChargerData/телеметрии — не падает", d is not None)
    check("ток −0.527 А, банок нет, потока нет",
          d["current"] == -0.527 and d["cells"] == [] and d["flow"] is None)
    check("батареи нет → None", platform_macos.parse_battery({"BatteryInstalled": False}) is None
          and platform_macos.parse_battery(None) is None)


def linux() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        def mk(name: str, **files) -> None:
            (base / name).mkdir()
            for k, v in files.items():
                (base / name / k).write_text(f"{v}\n")

        print("Linux · сервер без батареи")
        mk("AC", type="Mains", online=1)
        check("батареи нет → None", platform_linux.battery_detail(base) is None)
        check("нет папки → None", platform_linux.battery_detail(base / "нет") is None)

        print("Linux · ноутбук на зарядке (energy_*)")
        mk("BAT0", type="Battery", present=1, status="Charging", capacity=72,
           voltage_now=12650000, current_now=1830000, power_now=23150000,
           energy_now=41200000, energy_full=52800000, energy_full_design=57000000, cycle_count=312)
        mk("hidpp_battery_0", type="Battery", scope="Device", status="Discharging", capacity=40)
        d = platform_linux.battery_detail(base)
        check("заряд от блока", d["status"] == "charging" and d["source"] == "adapter")
        check("12.65 В, +1.83 А, +23.15 Вт", (d["voltage"], d["current"], d["power"]) == (12.65, 1.83, 23.15))
        check("ёмкость в Вт·ч и время до полного",
              d["unit"] == "Вт·ч" and d["cap_full"] == 52.8 and d["time_full"] == 30)

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        print("Linux · разряд (charge_*, ток со знаком минус)")
        (base / "BAT1").mkdir()
        for k, v in dict(type="Battery", status="Discharging", capacity=35, voltage_now=7410000,
                         current_now=-950000, charge_now=1650000, charge_full=4700000,
                         charge_full_design=5000000, temp=318).items():
            (base / "BAT1" / k).write_text(f"{v}\n")
        d = platform_linux.battery_detail(base)
        check("разряд −0.95 А, мощность ≈ −7.04 Вт",
              d["current"] == -0.95 and abs(d["power"] + 7.0395) < 1e-6)
        check("мА·ч целыми, время до разряда 104 мин",
              d["cap_now"] == 1650 and d["unit"] == "мА·ч" and d["time_empty"] == 104)
        check("температура 31.8 °C, блока нет", d["temp"] == 31.8 and d["adapter"] is None)


if __name__ == "__main__":
    mac()
    linux()
    print(f"\nbattery test: {'OK' if not FAILS else f'{len(FAILS)} FAIL'}")
    sys.exit(1 if FAILS else 0)
