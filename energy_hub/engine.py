"""Core aggregation engine — failover, aggregation, stale data policy."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from .adapters.base import AnkerGridReading, SensorReading, SolarbankReading
from .config import AppConfig
from .timeutil import parse_iso_epoch
from .validation import SensorValidator

logger = logging.getLogger("energy_hub.engine")


class Engine:
    """Central aggregation engine.

    Receives normalised readings from all adapters and maintains the current
    "state of the world" that the publisher uses to emit MQTT messages.
    """

    HISTORY_SIZE = 240

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
        # Charger power at the time the last grid reading was accepted; the difference
        # to the current charger power explains legitimate grid power jumps.
        self._charger_at_last_grid: float = 0.0

        # Keep short histories so residual loads can be derived from
        # temporally consistent samples instead of mixing meter and charger ticks.
        self._pv_history: deque[SensorReading] = deque(maxlen=self.HISTORY_SIZE)
        self._grid_tasmota_history: deque[SensorReading] = deque(maxlen=self.HISTORY_SIZE)
        self._grid_anker_history: deque[AnkerGridReading] = deque(maxlen=self.HISTORY_SIZE)
        self._solarbank_history: deque[SolarbankReading] = deque(maxlen=self.HISTORY_SIZE)
        self._charger_history: deque[tuple[float, float]] = deque(maxlen=self.HISTORY_SIZE)

        # --- grid failover tracking ---
        self._active_grid_source: str = ""  # "tasmota" or "anker"
        # When the current uninterrupted run of Tasmota grid readings started.
        self._tasmota_fresh_since: float = 0.0

    # -- public update methods -----------------------------------------------

    def update_pv(self, reading: SensorReading) -> None:
        ok, msg = self._validator.validate(reading, "pv")
        if not ok:
            logger.log(logging.INFO if msg.startswith("held:") else logging.WARNING, "PV reading: %s", msg)
            return
        self.pv_roof = reading
        self._pv_history.append(reading)

    def update_grid_tasmota(self, reading: SensorReading) -> None:
        expected_jump = self._current_charger_power() - self._charger_at_last_grid
        ok, msg = self._validator.validate(reading, "grid", expected_jump=expected_jump)
        if not ok:
            logger.log(logging.INFO if msg.startswith("held:") else logging.WARNING, "Grid reading: %s", msg)
            return
        prev = self.grid_tasmota
        if prev is None or reading.received_at - prev.received_at > self._cfg.sources.tasmota_grid.stale_timeout:
            self._tasmota_fresh_since = reading.received_at  # (re)start of a continuous run
        self.grid_tasmota = reading
        self._grid_tasmota_history.append(reading)
        self._charger_at_last_grid = self._current_charger_power()

    def _current_charger_power(self) -> float:
        """Charger power if the last charger message is not stale, else 0."""
        if not self.charger_received_at:
            return 0.0
        if time.time() - self.charger_received_at > self._cfg.sources.charger.stale_timeout:
            return 0.0
        return self.charger_power

    def update_grid_anker(self, reading: AnkerGridReading) -> None:
        self.grid_anker = reading
        self._grid_anker_history.append(reading)

    def update_solarbank(self, reading: SolarbankReading) -> None:
        self.solarbank = reading
        self._solarbank_history.append(reading)

    def update_charger(self, power: float) -> None:
        now = time.time()
        self.charger_power = power
        self.charger_received_at = now
        self._charger_history.append((now, power))

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

    def _switch_grid_source(self, source: str, why: str = "") -> None:
        if self._active_grid_source != source:
            if self._active_grid_source:
                logger.info("Grid source switched: %s → %s%s", self._active_grid_source, source,
                            f" ({why})" if why else "")
            self._active_grid_source = source

    def active_grid(self) -> tuple[SensorReading | None, str]:
        """Return (effective_grid_reading_as_SensorReading, source_name).

        Priority: Tasmota (fresh) → Anker (fresh) → whichever stale reading is newer.
        After Tasmota was stale, ``sources.tasmota_grid.failback_after`` seconds of
        continuous Tasmota data are required before it replaces a working Anker
        feed again, so a reader with flaky WiFi does not flap between meters.
        """
        cfg = self._cfg.sources
        tasmota_fresh = self.grid_tasmota is not None and not self._grid_is_stale(self.grid_tasmota)
        anker_fresh = self.grid_anker is not None and self.grid_anker.age() <= cfg.anker.stale_timeout

        if tasmota_fresh and self._active_grid_source == "anker" and anker_fresh:
            run = self.grid_tasmota.received_at - self._tasmota_fresh_since
            if run < cfg.tasmota_grid.failback_after:
                return self.grid_anker.to_sensor_reading(), "anker"  # hold until the run is long enough

        if tasmota_fresh:
            self._switch_grid_source("tasmota")
            return self.grid_tasmota, "tasmota"

        if anker_fresh:
            self._switch_grid_source("anker", "tasmota stale")
            return self.grid_anker.to_sensor_reading(), "anker"

        # Neither is fresh: use whichever stale reading is newer (snapshot() zeroes it
        # once it exceeds the grace deadline).
        candidates = [
            (r.received_at, name, r)
            for name, r in (("tasmota", self.grid_tasmota), ("anker", self.grid_anker))
            if r is not None
        ]
        if candidates:
            _, name, reading = max(candidates, key=lambda c: c[0])
            self._switch_grid_source(name, "both stale")
            return (reading if name == "tasmota" else reading.to_sensor_reading()), name

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

    def _reading_sample_time(
        self,
        reading: SensorReading | AnkerGridReading | SolarbankReading | None,
    ) -> float | None:
        if reading is None:
            return None
        if isinstance(reading, SensorReading) and reading.source in ("pv", "grid"):
            # Only trust the device clock while it agrees with the host clock.
            if self._validator.device_time_trusted(reading.source):
                return parse_iso_epoch(reading.timestamp) or reading.received_at
        return reading.received_at

    def _aligned_reading(
        self,
        history: deque,
        current: SensorReading | AnkerGridReading | SolarbankReading | None,
        reference_time: float | None,
    ) -> SensorReading | AnkerGridReading | SolarbankReading | None:
        if current is None or reference_time is None or not history:
            return current
        for reading in reversed(history):
            sample_time = self._reading_sample_time(reading)
            if sample_time is not None and sample_time <= reference_time:
                return reading
        return current  # nothing old enough: better current than the oldest sample

    def _aligned_charger_sample(self, reference_time: float | None) -> tuple[float, float]:
        if not self._charger_history:
            return 0.0, 0.0
        if reference_time is None:
            sample_time, power = self._charger_history[-1]
            return power, sample_time
        for sample_time, power in reversed(self._charger_history):
            if sample_time <= reference_time:
                return power, sample_time
        sample_time, power = self._charger_history[-1]
        return power, sample_time

    def _snapshot_reference_time(
        self,
        grid_reading: SensorReading | AnkerGridReading | None,
        grid_stale_timeout: int,
        pv_reading: SensorReading | None,
        solarbank: SolarbankReading | None,
    ) -> float | None:
        candidates: list[float] = []

        for reading, stale_timeout in (
            (grid_reading, grid_stale_timeout),
            (pv_reading, self._cfg.sources.tasmota_pv.stale_timeout),
            (solarbank, self._cfg.sources.anker.stale_timeout),
        ):
            if reading is None or reading.age() > self._stale_grace_deadline(stale_timeout):
                continue
            sample_time = self._reading_sample_time(reading)
            if sample_time is not None:
                candidates.append(sample_time)

        return min(candidates) if candidates else None

    # -- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Build the full aggregated snapshot for publishing."""
        grid_reading, grid_source = self.active_grid()

        grid_stale_timeout = (
            self._cfg.sources.anker.stale_timeout
            if grid_source == "anker"
            else self._cfg.sources.tasmota_grid.stale_timeout
        )
        reference_time = self._snapshot_reference_time(
            self.grid_anker if grid_source == "anker" else self.grid_tasmota,
            grid_stale_timeout,
            self.pv_roof,
            self.solarbank,
        )
        if reference_time is not None and time.time() - reference_time > self._cfg.validation.max_alignment_skew:
            # One source lags far behind (WiFi gap, clock problem): aligning everything
            # to it would publish old data, so use the latest readings instead.
            reference_time = None

        if grid_source == "anker":
            aligned_grid = self._aligned_reading(
                self._grid_anker_history,
                self.grid_anker,
                reference_time,
            )
            grid_reading = aligned_grid.to_sensor_reading() if aligned_grid else None
        elif grid_source == "tasmota":
            grid_reading = self._aligned_reading(
                self._grid_tasmota_history,
                self.grid_tasmota,
                reference_time,
            )

        pv_reading = self._aligned_reading(self._pv_history, self.pv_roof, reference_time)
        sb = self._aligned_reading(self._solarbank_history, self.solarbank, reference_time)

        # Grid
        if grid_reading:
            grid_age = grid_reading.age()
            grid_stale = grid_age > grid_stale_timeout
            grid_power = grid_reading.power
            if grid_age > self._stale_grace_deadline(grid_stale_timeout):
                grid_power = 0.0
        else:
            grid_power = 0.0
            grid_stale = True

        # Cumulative energy counters always come from the official (Tasmota SML) meter
        # when it has ever reported: a counter that pauses during an outage and then
        # jumps forward is correct, whereas switching to the Anker meter's own lifetime
        # totals would produce a step of tens of thousands of kWh in evcc and Grafana.
        if self.grid_tasmota is not None:
            energy_reading, energy_source = self.grid_tasmota, "tasmota"
            energy_stale = self._grid_is_stale(self.grid_tasmota)
        elif self.grid_anker is not None:
            energy_reading, energy_source = self.grid_anker.to_sensor_reading(), "anker"
            energy_stale = self.grid_anker.age() > self._cfg.sources.anker.stale_timeout
        else:
            energy_reading, energy_source, energy_stale = None, "", True

        # PV roof
        pv_power, pv_stale = self._effective_power(
            pv_reading, self._cfg.sources.tasmota_pv.stale_timeout,
        )

        # Solarbank / balcony
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
        now = time.time()
        raw_charger_stale = (now - self.charger_received_at) > self._cfg.sources.charger.stale_timeout if self.charger_received_at else True
        raw_charger = self.charger_power if not raw_charger_stale else 0.0
        charger_sample, charger_sample_time = self._aligned_charger_sample(reference_time)
        charger_stale = (now - charger_sample_time) > self._cfg.sources.charger.stale_timeout if charger_sample_time else True
        charger = charger_sample if not charger_stale else 0.0

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

        return {
            # Grid
            "grid": {
                "power": round(grid_power, 1),          # net: positive=import, negative=export
                "import_w": round(grid_import, 1),      # always positive when importing
                "export_w": round(grid_export, 1),      # always positive when exporting
                "energy_import": round(energy_reading.energy_import, 3) if energy_reading else 0.0,
                "energy_export": round(energy_reading.energy_export, 3) if energy_reading else 0.0,
                "energy_source": energy_source,   # meter the counters come from (normally "tasmota")
                "energy_stale": energy_stale,
                "source": grid_source,            # meter the power comes from
                "stale": grid_stale,
            },
            # PV roof
            "pv_roof": {
                "power": round(pv_power, 1),
                "energy": round(pv_reading.energy_import, 3) if pv_reading else 0.0,
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
                "raw_power": round(raw_charger, 1),
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
            info: dict[str, Any] = {
                "quality": round(self._validator.get_data_quality(name), 2),
                "validation": self._validator.stats(name),
            }
            if reading is None:
                return {"status": "no_data", "age": None, **info}
            age = now - reading.received_at
            return {"status": self._status_for_age(age, stale_timeout), "age": round(age, 1), **info}

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
