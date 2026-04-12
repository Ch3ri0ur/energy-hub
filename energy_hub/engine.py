"""Core aggregation engine — failover, aggregation, stale data policy."""

from __future__ import annotations

import logging
import time
from typing import Any

from .adapters.base import (
    AnkerGridReading,
    SensorReading,
    SensorValidator,
    SolarbankReading,
)
from .config import AppConfig

logger = logging.getLogger("energy_hub.engine")


class Engine:
    """Central aggregation engine.

    Receives normalised readings from all adapters and maintains the current
    "state of the world" that the publisher uses to emit MQTT messages.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._validator = SensorValidator(cfg.validation)

        # --- latest validated readings (None = never received) ---
        self.pv_roof: SensorReading | None = None
        self.grid_tasmota: SensorReading | None = None
        self.grid_anker: AnkerGridReading | None = None
        self.solarbank: SolarbankReading | None = None
        self.charger_power: float = 0.0
        self.charger_received_at: float = 0.0

        # --- error counters ---
        self._pv_errors = 0
        self._grid_errors = 0

        # --- grid failover tracking ---
        self._active_grid_source: str = ""  # "tasmota" or "anker"

    # -- public update methods -----------------------------------------------

    def update_pv(self, reading: SensorReading) -> None:
        ok, msg = self._validator.validate(reading, self.pv_roof, "pv")
        if not ok:
            self._pv_errors += 1
            if msg:
                logger.warning("PV validation (%d/%d): %s",
                               self._pv_errors, self._cfg.validation.max_consecutive_errors, msg)
            if self._pv_errors < self._cfg.validation.max_consecutive_errors:
                return
            logger.warning("Accepting PV data after %d consecutive errors", self._pv_errors)
        self._pv_errors = 0
        self.pv_roof = reading

    def update_grid_tasmota(self, reading: SensorReading) -> None:
        ok, msg = self._validator.validate(reading, self.grid_tasmota, "grid")
        if not ok:
            self._grid_errors += 1
            if msg:
                logger.warning("Grid validation (%d/%d): %s",
                               self._grid_errors, self._cfg.validation.max_consecutive_errors, msg)
            if self._grid_errors < self._cfg.validation.max_consecutive_errors:
                return
            logger.warning("Accepting grid data after %d consecutive errors", self._grid_errors)
        self._grid_errors = 0
        self.grid_tasmota = reading

    def update_grid_anker(self, reading: AnkerGridReading) -> None:
        self.grid_anker = reading

    def update_solarbank(self, reading: SolarbankReading) -> None:
        self.solarbank = reading

    def update_charger(self, power: float) -> None:
        self.charger_power = power
        self.charger_received_at = time.time()

    # -- grid failover -------------------------------------------------------

    def _grid_is_stale(self, reading: SensorReading | AnkerGridReading | None) -> bool:
        if reading is None:
            return True
        return reading.age() > self._cfg.sources.tasmota_grid.stale_timeout

    def _stale_grace_deadline(self, stale_timeout: int) -> int:
        return max(
            stale_timeout,
            min(
                self._cfg.stale_policy.zero_after,
                stale_timeout + self._cfg.stale_policy.grace_period,
            ),
        )

    def _status_for_age(self, age: float, stale_timeout: int) -> str:
        if age <= stale_timeout:
            return "ok"
        if age <= self._stale_grace_deadline(stale_timeout):
            return "stale"
        return "dead"

    def active_grid(self) -> tuple[SensorReading | None, str]:
        """Return (effective_grid_reading_as_SensorReading, source_name).

        Priority: Tasmota (if fresh) → Anker (if fresh) → Tasmota stale → None.
        """
        if self.grid_tasmota and not self._grid_is_stale(self.grid_tasmota):
            if self._active_grid_source != "tasmota":
                if self._active_grid_source:
                    logger.info("Grid source switched: %s → tasmota", self._active_grid_source)
                self._active_grid_source = "tasmota"
            return self.grid_tasmota, "tasmota"

        if self.grid_anker and self.grid_anker.age() < self._cfg.sources.anker.stale_timeout:
            if self._active_grid_source != "anker":
                logger.info("Grid source switched: %s → anker (tasmota stale)", self._active_grid_source)
                self._active_grid_source = "anker"
            return self.grid_anker.to_sensor_reading(), "anker"

        # Tasmota stale but still the best we have
        if self.grid_tasmota:
            self._active_grid_source = "tasmota"
            return self.grid_tasmota, "tasmota"

        self._active_grid_source = ""
        return None, ""

    # -- stale data helpers --------------------------------------------------

    def _effective_power(self, reading: SensorReading | None, stale_timeout: int) -> tuple[float, bool]:
        """Return (power, is_stale).  Zero after grace period."""
        if reading is None:
            return 0.0, True
        age = reading.age()
        if age <= stale_timeout:
            return reading.power, False
        if age <= self._stale_grace_deadline(stale_timeout):
            return reading.power, True
        return 0.0, True

    # -- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Build the full aggregated snapshot for publishing."""
        grid_reading, grid_source = self.active_grid()

        # Grid
        if grid_reading:
            grid_stale_timeout = (
                self._cfg.sources.anker.stale_timeout
                if grid_source == "anker"
                else self._cfg.sources.tasmota_grid.stale_timeout
            )
            grid_age = grid_reading.age()
            grid_stale = grid_age > grid_stale_timeout
            grid_power = grid_reading.power
            if grid_age > self._stale_grace_deadline(grid_stale_timeout):
                grid_power = 0.0
        else:
            grid_power = 0.0
            grid_stale = True

        # PV roof
        pv_power, pv_stale = self._effective_power(
            self.pv_roof, self._cfg.sources.tasmota_pv.stale_timeout,
        )

        # Solarbank / balcony
        sb = self.solarbank
        sb_age = sb.age() if sb else 0.0
        sb_stale = sb is None or sb_age > self._cfg.sources.anker.stale_timeout
        sb_zeroed = sb is None or sb_age > self._stale_grace_deadline(self._cfg.sources.anker.stale_timeout)
        sb_output = 0.0 if sb_zeroed or sb is None else sb.output_power
        sb_generation = 0.0 if sb_zeroed or sb is None else sb.photovoltaic_power
        sb_pv_1_power = 0.0 if sb_zeroed or sb is None else sb.pv_1_power
        sb_pv_2_power = 0.0 if sb_zeroed or sb is None else sb.pv_2_power
        sb_pv_3_power = 0.0 if sb_zeroed or sb is None else sb.pv_3_power
        sb_pv_4_power = 0.0 if sb_zeroed or sb is None else sb.pv_4_power

        # Charger
        charger_stale = (time.time() - self.charger_received_at) > self._cfg.sources.charger.stale_timeout if self.charger_received_at else True
        charger = self.charger_power if not charger_stale else 0.0

        # Aggregation
        total_pv = pv_power + sb_output          # AC-bus contribution (what reaches the house)
        total_consumption = grid_power + total_pv  # = house + charger
        house_load = total_consumption - charger

        # Pre-split fields for Grafana stacked charts (avoids transform overrides)
        grid_import = max(0.0, grid_power)
        grid_export = max(0.0, -grid_power)
        battery_power_val = 0.0 if sb_zeroed else (sb.battery_power_signed if sb else 0.0)
        battery_charge = max(0.0, battery_power_val)
        battery_discharge = max(0.0, -battery_power_val)
        # Actual panel generation before battery buffering
        sb_pv_gen = sb_generation
        pv_total_generation = pv_power + sb_pv_gen
        battery_state = sb.battery_state if sb and not sb_zeroed else "unknown"

        # Self-consumption: what fraction of solar is consumed locally (not exported to grid)?
        if total_pv > 0:
            self_consumed_solar = max(0.0, total_pv - grid_export)
            self_consumption_pct = round(self_consumed_solar / total_pv * 100, 1)
        else:
            self_consumption_pct = 0.0

        # Self-sufficiency: what fraction of total demand is covered by solar?
        if total_consumption > 0:
            self_sufficiency_pct = round(min(total_pv, total_consumption) / total_consumption * 100, 1)
        else:
            self_sufficiency_pct = 0.0

        now = time.time()
        return {
            # Grid
            "grid": {
                "power": round(grid_power, 1),          # net: positive=import, negative=export
                "import_w": round(grid_import, 1),      # always positive when importing
                "export_w": round(grid_export, 1),      # always positive when exporting
                "energy_import": round(grid_reading.energy_import, 3) if grid_reading else 0.0,
                "energy_export": round(grid_reading.energy_export, 3) if grid_reading else 0.0,
                "source": grid_source,
                "stale": grid_stale,
            },
            # PV roof
            "pv_roof": {
                "power": round(pv_power, 1),
                "energy": round(self.pv_roof.energy_import, 3) if self.pv_roof else 0.0,
                "stale": pv_stale,
            },
            # PV balcony (solarbank)
            "pv_balcony": {
                "power": round(sb_generation, 1),
                "pv_1_power": round(sb_pv_1_power, 1),
                "pv_2_power": round(sb_pv_2_power, 1),
                "pv_3_power": round(sb_pv_3_power, 1),
                "pv_4_power": round(sb_pv_4_power, 1),
                "output_power": round(sb_output, 1),
                "stale": sb_stale,
            },
            # Total PV
            "pv_total": {
                "power": round(total_pv, 1),                       # AC-bus contribution (house balance)
                "generation": round(pv_total_generation, 1),       # actual panel output (roof + balcony panels)
            },
            # Battery
            "battery": {
                "soc": round(sb.battery_soc, 1) if sb and not sb_zeroed else 0.0,
                "power": round(battery_power_val, 1),         # signed: positive=charging, negative=discharging
                "charge_w": round(battery_charge, 1),        # always positive when charging
                "discharge_w": round(battery_discharge, 1),  # always positive when discharging
                "energy_wh": round(sb.battery_energy_wh, 1) if sb and not sb_zeroed else 0.0,
                "state": battery_state,
                "stale": sb_stale,
            },
            # Charger
            "charger": {
                "power": round(charger, 1),
                "stale": charger_stale,
            },
            # House load
            "house": {
                "power": round(house_load, 1),
            },
            # Totals
            "consumption": round(total_consumption, 1),
            "self_consumption_pct": self_consumption_pct,
            "self_sufficiency_pct": self_sufficiency_pct,
            "timestamp": now,
        }

    # -- health info ---------------------------------------------------------

    def source_statuses(self) -> dict[str, Any]:
        """Per-source health info for the health publisher."""
        now = time.time()

        def _status(name: str, reading, stale_timeout: int) -> dict[str, Any]:
            if reading is None:
                return {"status": "no_data", "age": None, "quality": self._validator.get_data_quality(name)}
            age = now - reading.received_at
            status = self._status_for_age(age, stale_timeout)
            return {"status": status, "age": round(age, 1), "quality": round(self._validator.get_data_quality(name), 2)}

        return {
            "pv_roof": _status("pv", self.pv_roof, self._cfg.sources.tasmota_pv.stale_timeout),
            "grid_tasmota": _status("grid", self.grid_tasmota, self._cfg.sources.tasmota_grid.stale_timeout),
            "grid_anker": {
                "status": self._status_for_age(self.grid_anker.age(), self._cfg.sources.anker.stale_timeout) if self.grid_anker else "no_data",
                "age": round(self.grid_anker.age(), 1) if self.grid_anker else None,
            },
            "solarbank": {
                "status": self._status_for_age(self.solarbank.age(), self._cfg.sources.anker.stale_timeout) if self.solarbank else "no_data",
                "age": round(self.solarbank.age(), 1) if self.solarbank else None,
            },
            "charger": {
                "status": self._status_for_age(now - self.charger_received_at, self._cfg.sources.charger.stale_timeout) if self.charger_received_at else "no_data",
                "age": round(now - self.charger_received_at, 1) if self.charger_received_at else None,
            },
            "active_grid_source": self._active_grid_source,
        }
