"""Base adapter and shared data types for sensor readings."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..config import ValidationConfig

logger = logging.getLogger("energy_hub.adapters")


def _parse_timestamp_epoch(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


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
    ac_output_power_signed: float = 0.0
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


# ---------------------------------------------------------------------------
# Validator for Tasmota SML sensors (reused from mqtt4.py logic)
# ---------------------------------------------------------------------------


class SensorValidator:
    """Validate Tasmota SML readings: bounds, rate-of-change, power jumps."""

    def __init__(self, cfg: ValidationConfig) -> None:
        self._cfg = cfg
        # source -> list of (is_valid, timestamp)
        self._history: dict[str, list[tuple[bool, float]]] = {}
        # source -> list of SensorReading
        self._value_history: dict[str, list[SensorReading]] = {}
        # source -> baseline reading
        self._baseline: dict[str, SensorReading | None] = {}
        self._disagreement: dict[str, int] = {}

    def get_data_quality(self, source: str) -> float:
        hist = self._history.get(source, [])[-self._cfg.quality_check_window:]
        if not hist:
            return 1.0
        return sum(1 for ok, _ in hist if ok) / len(hist)

    def validate(
        self,
        data: SensorReading,
        previous: SensorReading | None,
        source: str,
    ) -> tuple[bool, str | None]:
        cfg = self._cfg
        effective_prev = previous or self._baseline.get(source)
        reading_epoch = _parse_timestamp_epoch(data.timestamp) if data.timestamp else None

        # --- static bounds ---
        ok, msg = True, None
        if (
            reading_epoch is not None
            and (data.received_at - reading_epoch) > cfg.stale_data_threshold
        ):
            ok, msg = False, f"{source}: reading timestamp stale"
        elif data.energy_import < 0:
            ok, msg = False, f"{source}: energy_import negative"
        elif data.energy_export < 0:
            ok, msg = False, f"{source}: energy_export negative"
        elif data.energy_import > cfg.max_total_energy:
            ok, msg = False, f"{source}: energy_import too high ({data.energy_import})"
        elif data.energy_export > cfg.max_total_energy:
            ok, msg = False, f"{source}: energy_export too high ({data.energy_export})"
        elif data.power < cfg.min_power:
            ok, msg = False, f"{source}: power too low ({data.power})"
        elif data.power > cfg.max_power:
            ok, msg = False, f"{source}: power too high ({data.power})"

        # --- rate-of-change ---
        if ok and effective_prev is not None:
            tol = cfg.meter_noise_tolerance
            td = max(data.received_at - effective_prev.received_at, 10)
            max_rate = cfg.max_meter_rate_kwh_per_10s * (td / 10.0)

            if data.energy_import < 1000 < effective_prev.energy_import:
                ok, msg = False, f"{source}: energy_import reset"
            elif data.energy_export < 1000 < effective_prev.energy_export:
                ok, msg = False, f"{source}: energy_export reset"
            elif data.energy_import < effective_prev.energy_import - tol:
                ok, msg = False, f"{source}: energy_import decreased"
            elif data.energy_export < effective_prev.energy_export - tol:
                ok, msg = False, f"{source}: energy_export decreased"
            elif data.energy_import - effective_prev.energy_import > max_rate:
                ok, msg = False, f"{source}: energy_import rising too fast"
            elif data.energy_export - effective_prev.energy_export > max_rate:
                ok, msg = False, f"{source}: energy_export rising too fast"

        # --- power jump from median ---
        if ok:
            median = self._median_power(source)
            if median is not None and abs(data.power - median) > cfg.max_power_jump:
                ok, msg = False, f"{source}: power jump too large ({data.power}W vs median {median}W)"

        # --- source-specific ---
        if ok:
            is_grid = data.energy_export > 0
            if is_grid:
                if data.power < cfg.grid_min_power:
                    ok, msg = False, f"{source}: grid power too low ({data.power})"
                elif effective_prev is None:
                    if data.energy_import < cfg.grid_min_total_in:
                        ok, msg = False, f"{source}: grid energy_import too low"
                    elif data.energy_export < cfg.grid_min_total_out:
                        ok, msg = False, f"{source}: grid energy_export too low"
            else:
                if data.power > cfg.pv_max_power:
                    ok, msg = False, f"{source}: PV power too high ({data.power})"
                elif data.power < 0:
                    ok, msg = False, f"{source}: PV power negative"
                elif effective_prev is None and data.energy_import < cfg.pv_min_total_in:
                    ok, msg = False, f"{source}: PV energy_import too low"

        # --- recovery ---
        if not ok and self._disagreement.get(source, 0) >= cfg.recovery_consensus_count:
            hist = self._value_history.get(source, [])
            if hist:
                logger.warning("%s: auto-recovering baseline after %d disagreements",
                               source, self._disagreement[source])
                self._baseline[source] = hist[-1]
                self._disagreement[source] = 0
                return self.validate(data, self._baseline[source], source)

        # --- update tracking ---
        self._history.setdefault(source, []).append((ok, time.time()))
        if ok:
            vhist = self._value_history.setdefault(source, [])
            vhist.append(data)
            if len(vhist) > cfg.value_history_size:
                self._value_history[source] = vhist[-cfg.value_history_size:]

        self._update_baseline(data, source, ok)
        return ok, msg

    def _median_power(self, source: str) -> float | None:
        hist = self._value_history.get(source, [])
        if len(hist) < 3:
            return None
        powers = sorted(r.power for r in hist[-self._cfg.value_history_size:])
        return powers[len(powers) // 2]

    def _update_baseline(self, data: SensorReading, source: str, ok: bool) -> None:
        if self._baseline.get(source) is None or ok:
            self._baseline[source] = data
            self._disagreement[source] = 0
            return
        hist = self._value_history.get(source, [])
        if len(hist) >= 2:
            r = hist[-2:]
            consistent = (
                abs(r[0].energy_import - r[1].energy_import)
                <= self._cfg.max_meter_rate_kwh_per_10s * 3
            )
            if consistent:
                self._disagreement[source] = self._disagreement.get(source, 0) + 1
            else:
                self._disagreement[source] = 0
