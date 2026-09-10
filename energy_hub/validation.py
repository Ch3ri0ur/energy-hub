"""Plausibility validation for Tasmota SML meter readings.

Rules, checked in this order (first failure wins):

1. ``bounds``   – power and cumulative energies inside the physical limits for the
                  source (grid: import ≤ max_power, export ≥ grid_min_power;
                  PV: 0 ≤ power ≤ pv_max_power; energies in [0, max_total_energy]).
2. ``first``    – for the very first accepted reading of a source the cumulative
                  energies must be at least the configured minimums.  Catches decode
                  garbage and "wrong meter" right after boot.
3. ``monotonic``– cumulative energies never decrease (beyond ``meter_noise_tolerance``)
                  compared with the last *accepted* reading.
4. ``rate``     – cumulative energies never increase faster than physically possible:
                  ``max_power * elapsed * rate_safety_factor`` (+ tolerance).  Because
                  the limit scales with elapsed time, long WiFi gaps are fine.

Rules 3 and 4 compare against the last accepted reading.

Garbage model
-------------
The IR readers occasionally decode garbage: values orders of magnitude off (caught by
rules 1–4) but also values that are only slightly wrong.  A slightly wrong value that
gets accepted would poison the baseline, so two mechanisms limit the damage:

* ``confirmation`` – a reading that fails rule 3/4 is not discarded outright but held
  as a *candidate*.  If the next reading is consistent with the candidate (rules 3/4
  pass against it and the power is close), the candidate regime is confirmed and the
  new reading becomes the baseline.  If the next reading is consistent with the old
  baseline instead, the candidate was garbage and is dropped.  Recovery from a
  poisoned baseline therefore costs one or two readings, not ``max_consecutive_errors``.
* ``power jump`` – a reading whose power differs from the last accepted power by more
  than ``power_jump_confirm`` W is held the same way and only published once a second
  reading confirms the new level, so a single wrong power value never reaches the
  output.  The caller may pass ``expected_jump`` (W) for changes it can explain — the
  engine passes the change in charger power since the last accepted grid reading — and
  that amount is exempt, so a car starting to charge at 11 kW passes immediately.
  Unexplained jumps are delayed by one reading.

``max_consecutive_errors`` remains as a last-resort override for rules 3/4.  Bounds and
first-reading failures are never accepted through any of these paths.

Device timestamps are **never** a reason to reject a reading (a Tasmota that lost NTP
after a WiFi drop still delivers correct meter values).  Instead the offset between
device clock and host clock is tracked per source (`clock_offset`) so the engine can
decide whether to trust device time for sample alignment.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

from .adapters.base import SensorReading
from .config import ValidationConfig
from .timeutil import parse_iso_epoch

logger = logging.getLogger("energy_hub.validation")

GRID = "grid"
PV = "pv"

_BERLIN_HINT = (
    "check the Tasmota Timezone/NTP settings "
    "(for Berlin: Timezone 99; TimeDst 0,0,3,1,2,120; TimeStd 0,0,10,1,3,60)"
)


@dataclass
class SourceStats:
    """Per-source bookkeeping."""

    accepted: int = 0
    rejected: int = 0
    held: int = 0
    consecutive_errors: int = 0
    last_accepted: SensorReading | None = None
    candidate: SensorReading | None = None
    clock_offset: float | None = None      # host receive time − device time (s)
    clock_warned: bool = False
    quality: deque[bool] = field(default_factory=deque)


class SensorValidator:
    """Validate Tasmota SML readings for the ``grid`` and ``pv`` sources."""

    def __init__(self, cfg: ValidationConfig) -> None:
        self._cfg = cfg
        self._stats: dict[str, SourceStats] = {}

    # -- public API ----------------------------------------------------------

    def validate(
        self, reading: SensorReading, source: str, expected_jump: float = 0.0
    ) -> tuple[bool, str | None]:
        """Return ``(accepted, reason)``.  ``reason`` is ``None`` when accepted.

        ``expected_jump`` is a signed power change (W) the caller can explain by
        other measurements; it is subtracted before the power-jump check.
        Held readings (awaiting confirmation) return ``(False, "held: ...")``; they
        count as neither accepted nor rejected for the quality ratio.
        """
        st = self._stats.setdefault(
            source, SourceStats(quality=deque(maxlen=self._cfg.quality_check_window))
        )
        self._track_clock(reading, source, st)

        # -- hard checks: never recoverable through confirmation ----------------
        reason = self._check_bounds(reading, source)
        if reason is None and st.last_accepted is None:
            reason = self._check_first(reading, source)
        if reason is not None:
            return self._reject(st, reason)

        prev = st.last_accepted
        if prev is None:
            return self._accept(st, reading)

        # -- soft checks: progression + power jump, with confirmation -------------
        reason = self._check_progression(reading, prev, source)
        if reason is None:
            # Residual change the caller cannot explain (signed: a charger that stops
            # explains a drop, not a rise of the same size).
            jump = abs((reading.power - prev.power) - expected_jump)
            elapsed = reading.received_at - prev.received_at
            # After a long gap (reader outage) the previous power says nothing about
            # the present; holding the first reading would only delay the fail-back.
            gap = self._cfg.power_jump_max_gap
            if self._cfg.power_jump_confirm and (not gap or elapsed <= gap) and jump > self._cfg.power_jump_confirm:
                if st.candidate is not None and self._confirms(reading, st.candidate, source):
                    logger.info("%s: power jump to %.0f W confirmed", source, reading.power)
                    return self._accept(st, reading)
                return self._hold(
                    st, reading,
                    f"{source}: unexplained power jump {prev.power:.0f} → {reading.power:.0f} W"
                    + (f" (charger explains {abs(expected_jump):.0f} W)" if expected_jump else ""),
                )
            return self._accept(st, reading)

        if st.candidate is not None and self._confirms(reading, st.candidate, source):
            logger.warning(
                "%s: re-baselined after confirmation (import %.3f → %.3f kWh, export %.3f → %.3f kWh)",
                source, prev.energy_import, reading.energy_import, prev.energy_export, reading.energy_export,
            )
            return self._accept(st, reading)

        st.consecutive_errors += 1
        if st.consecutive_errors >= self._cfg.max_consecutive_errors:
            logger.warning(
                "%s: accepting reading as new baseline after %d consecutive rejections (last reason: %s)",
                source, st.consecutive_errors, reason,
            )
            return self._accept(st, reading)
        return self._hold(st, reading, reason)

    # -- outcome helpers -------------------------------------------------------

    def _accept(self, st: SourceStats, reading: SensorReading) -> tuple[bool, None]:
        st.accepted += 1
        st.consecutive_errors = 0
        st.last_accepted = reading
        st.candidate = None
        st.quality.append(True)
        return True, None

    def _reject(self, st: SourceStats, reason: str) -> tuple[bool, str]:
        st.rejected += 1
        st.quality.append(False)
        return False, reason

    def _hold(self, st: SourceStats, reading: SensorReading, reason: str) -> tuple[bool, str]:
        st.held += 1
        st.candidate = reading
        return False, f"held: {reason} (awaiting confirmation)"

    def _confirms(self, reading: SensorReading, candidate: SensorReading, source: str) -> bool:
        """True when *reading* is consistent with the held *candidate*."""
        if self._check_progression(reading, candidate, source) is not None:
            return False
        limit = self._cfg.power_jump_confirm
        return not limit or abs(reading.power - candidate.power) <= limit

    def get_data_quality(self, source: str) -> float:
        st = self._stats.get(source)
        if not st or not st.quality:
            return 1.0
        return sum(st.quality) / len(st.quality)

    def clock_offset(self, source: str) -> float | None:
        st = self._stats.get(source)
        return st.clock_offset if st else None

    def device_time_trusted(self, source: str) -> bool:
        """True when the device clock is close enough to the host clock to use it
        for sample alignment: within ``clock_offset_warn`` *and* within
        ``max_alignment_skew`` (an offset larger than the skew would disable
        alignment on every snapshot anyway)."""
        off = self.clock_offset(source)
        limit = min(self._cfg.clock_offset_warn, self._cfg.max_alignment_skew)
        return off is not None and abs(off) <= limit

    def stats(self, source: str) -> dict[str, object]:
        st = self._stats.get(source)
        if not st:
            return {"accepted": 0, "rejected": 0, "held": 0, "consecutive_errors": 0, "clock_offset": None}
        return {
            "accepted": st.accepted,
            "rejected": st.rejected,
            "held": st.held,
            "consecutive_errors": st.consecutive_errors,
            "clock_offset": None if st.clock_offset is None else round(st.clock_offset, 1),
        }

    # -- rules ----------------------------------------------------------------

    def _check_bounds(self, r: SensorReading, source: str) -> str | None:
        cfg = self._cfg
        if not all(math.isfinite(x) for x in (r.power, r.energy_import, r.energy_export)):
            # json.loads happily produces NaN/Infinity; every comparison below is
            # False for NaN, so it would sail through and end up in the output JSON.
            return f"{source}: non-finite value ({r.power}/{r.energy_import}/{r.energy_export})"
        if r.energy_import < 0 or r.energy_export < 0:
            return f"{source}: negative energy counter ({r.energy_import}/{r.energy_export})"
        if r.energy_import > cfg.max_total_energy or r.energy_export > cfg.max_total_energy:
            return f"{source}: energy counter above max_total_energy ({r.energy_import}/{r.energy_export})"
        if source == GRID:
            if r.power > cfg.max_power:
                return f"{source}: import power above max_power ({r.power} W)"
            if r.power < cfg.grid_min_power:
                return f"{source}: export power below grid_min_power ({r.power} W)"
        else:
            if r.power < 0:
                return f"{source}: PV power negative ({r.power} W)"
            if r.power > cfg.pv_max_power:
                return f"{source}: PV power above pv_max_power ({r.power} W)"
        return None

    def _check_first(self, r: SensorReading, source: str) -> str | None:
        cfg = self._cfg
        if source == GRID:
            if r.energy_import < cfg.grid_min_total_in:
                return f"{source}: first energy_import below grid_min_total_in ({r.energy_import})"
            if r.energy_export < cfg.grid_min_total_out:
                return f"{source}: first energy_export below grid_min_total_out ({r.energy_export})"
        elif r.energy_import < cfg.pv_min_total_in:
            return f"{source}: first energy_import below pv_min_total_in ({r.energy_import})"
        return None

    def _check_progression(self, r: SensorReading, prev: SensorReading, source: str) -> str | None:
        cfg = self._cfg
        tol = cfg.meter_noise_tolerance
        elapsed = max(r.received_at - prev.received_at, 1.0)

        if r.energy_import < prev.energy_import - tol:
            return f"{source}: energy_import decreased ({prev.energy_import} → {r.energy_import})"
        if r.energy_export < prev.energy_export - tol:
            return f"{source}: energy_export decreased ({prev.energy_export} → {r.energy_export})"

        import_limit_w = cfg.max_power if source == GRID else cfg.pv_max_power
        export_limit_w = abs(cfg.grid_min_power) if source == GRID else 0.0
        max_import_step = import_limit_w * elapsed / 3.6e6 * cfg.rate_safety_factor + tol
        max_export_step = export_limit_w * elapsed / 3.6e6 * cfg.rate_safety_factor + tol

        if r.energy_import - prev.energy_import > max_import_step:
            return (f"{source}: energy_import rose {r.energy_import - prev.energy_import:.3f} kWh "
                    f"in {elapsed:.0f}s (limit {max_import_step:.3f})")
        if r.energy_export - prev.energy_export > max_export_step:
            return (f"{source}: energy_export rose {r.energy_export - prev.energy_export:.3f} kWh "
                    f"in {elapsed:.0f}s (limit {max_export_step:.3f})")
        return None

    # -- clock diagnostics ----------------------------------------------------

    def _track_clock(self, r: SensorReading, source: str, st: SourceStats) -> None:
        epoch = parse_iso_epoch(r.timestamp)
        if epoch is None:
            st.clock_offset = None
            return
        st.clock_offset = r.received_at - epoch
        if abs(st.clock_offset) > self._cfg.clock_offset_warn:
            if not st.clock_warned:
                logger.warning(
                    "%s: device time %r is %.0fs %s host time — device timestamps will "
                    "not be used for alignment; %s",
                    source, r.timestamp, abs(st.clock_offset),
                    "behind" if st.clock_offset > 0 else "ahead of", _BERLIN_HINT,
                )
                st.clock_warned = True
        elif st.clock_warned:
            logger.info("%s: device clock back in sync (offset %.0fs)", source, st.clock_offset)
            st.clock_warned = False
