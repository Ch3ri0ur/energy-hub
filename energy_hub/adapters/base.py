"""Shared data types for sensor readings."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Normalized reading types
# ---------------------------------------------------------------------------


@dataclass
class SensorReading:
    """Normalized power/energy reading from any source."""

    power: float = 0.0              # instantaneous watts
    energy_import: float = 0.0      # cumulative kWh (grid import or PV generation)
    energy_export: float = 0.0      # cumulative kWh (grid export, 0 for PV)
    timestamp: str = ""             # ISO or device timestamp
    received_at: float = field(default_factory=time.time)
    source: str = ""                # adapter name

    def age(self) -> float:
        return time.time() - self.received_at


@dataclass
class AnkerGridReading:
    """Grid-specific reading from the Anker smart meter."""

    grid_to_home_power: float = 0.0
    pv_to_grid_power: float = 0.0
    net_grid_power: float = 0.0
    grid_import_energy: float = 0.0
    grid_export_energy: float = 0.0
    received_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return time.time() - self.received_at

    def to_sensor_reading(self) -> SensorReading:
        return SensorReading(
            power=self.net_grid_power,
            energy_import=self.grid_import_energy,
            energy_export=self.grid_export_energy,
            received_at=self.received_at,
            source="anker_grid",
        )


@dataclass
class SolarbankReading:
    """Solarbank-specific reading (PV + battery + output)."""

    photovoltaic_power: float = 0.0
    pv_1_power: float = 0.0
    pv_2_power: float = 0.0
    pv_3_power: float = 0.0
    pv_4_power: float = 0.0
    battery_soc: float = 0.0
    battery_power_signed: float = 0.0   # positive = charging, negative = discharging
    battery_energy_wh: float = 0.0
    battery_capacity_wh: float = 0.0
    output_power: float = 0.0           # what reaches the house (≤800W)
    ac_output_power_signed: float = 0.0  # negative = Solarbank charging from the AC bus
    grid_to_battery_power: float = 0.0
    home_demand: float = 0.0
    temperature: float = 0.0
    received_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return time.time() - self.received_at

    @property
    def battery_state(self) -> str:
        if self.battery_power_signed > 10:
            return "charging"
        if self.battery_power_signed < -10:
            return "discharging"
        return "idle"
