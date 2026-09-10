import logging
import time
from datetime import datetime

import pytest

from energy_hub.adapters.base import SensorReading
from energy_hub.config import ValidationConfig
from energy_hub.validation import SensorValidator

T0 = 1_800_000_000.0


def grid(power=300.0, imp=42001.0, exp=28485.0, at=T0, ts=""):
    return SensorReading(power=power, energy_import=imp, energy_export=exp, timestamp=ts, received_at=at, source="grid")


def pv(power=1000.0, imp=60001.0, at=T0, ts=""):
    return SensorReading(power=power, energy_import=imp, energy_export=0.0, timestamp=ts, received_at=at, source="pv")


@pytest.fixture
def v():
    return SensorValidator(ValidationConfig())


# --- bounds ---------------------------------------------------------------

def test_first_good_readings_accepted(v):
    assert v.validate(grid(), "grid") == (True, None)
    assert v.validate(pv(), "pv") == (True, None)


@pytest.mark.parametrize("reading, source, fragment", [
    (grid(imp=-1), "grid", "negative energy"),
    (grid(imp=200000), "grid", "max_total_energy"),
    (grid(power=40000), "grid", "max_power"),
    (grid(power=-6000), "grid", "grid_min_power"),
    (pv(power=-5), "pv", "PV power negative"),
    (pv(power=9000), "pv", "pv_max_power"),
])
def test_bounds_rejected(v, reading, source, fragment):
    ok, msg = v.validate(reading, source)
    assert not ok and fragment in msg


def test_grid_with_zero_export_uses_grid_rules(v):
    cfg = ValidationConfig(grid_min_total_out=0.0)
    v = SensorValidator(cfg)
    ok, _ = v.validate(grid(power=-200, exp=0.0), "grid")
    assert ok


# --- first reading floor ----------------------------------------------------

def test_first_reading_floor_then_good_reading_accepted_immediately(v):
    ok, msg = v.validate(grid(imp=1.0, exp=1.0), "grid")
    assert not ok and "grid_min_total_in" in msg
    # garbage must not become the baseline: the next good reading passes at once
    assert v.validate(grid(at=T0 + 10), "grid") == (True, None)


def test_floor_only_applies_to_first_reading(v):
    v.validate(grid(), "grid")
    # a later reading below the floor is caught by monotonic, not by the floor
    ok, msg = v.validate(grid(imp=100.0, at=T0 + 10), "grid")
    assert not ok and "decreased" in msg


# --- progression ------------------------------------------------------------

def test_small_backwards_jitter_tolerated(v):
    v.validate(grid(imp=42001.0), "grid")
    assert v.validate(grid(imp=42000.95, at=T0 + 10), "grid")[0]


def test_energy_decrease_rejected(v):
    v.validate(grid(), "grid")
    ok, msg = v.validate(grid(imp=41990.0, at=T0 + 10), "grid")
    assert not ok and "energy_import decreased" in msg


def test_rate_limit_scales_with_elapsed_time(v):
    v.validate(grid(imp=42001.0), "grid")
    # 30 kW for 10 s = 0.083 kWh; ×2 + 0.1 tolerance ≈ 0.27 kWh allowed
    assert not v.validate(grid(imp=42001.5, at=T0 + 10), "grid")[0]
    # the same 0.5 kWh after a 10-minute WiFi gap (5 kWh allowed) is fine
    assert v.validate(grid(imp=42001.5, at=T0 + 600), "grid")[0]


def test_export_rate_uses_export_limit(v):
    v.validate(grid(exp=28485.0), "grid")
    # 5.6 kW for 10 s = 0.0156 kWh; ×2 + 0.1 = 0.13 allowed
    assert not v.validate(grid(exp=28485.2, at=T0 + 10), "grid")[0]
    assert v.validate(grid(exp=28485.1, at=T0 + 10), "grid")[0]


def test_single_garbage_counter_is_held_and_dropped(v):
    """Garbage counter value (order of magnitude off) followed by real data: nothing lost."""
    v.validate(grid(imp=42001.0), "grid")
    ok, msg = v.validate(grid(imp=4200.1, at=T0 + 10), "grid")    # decimal shift
    assert not ok and msg.startswith("held:")
    assert v.validate(grid(imp=42001.1, at=T0 + 20), "grid")[0]  # consistent with baseline → accepted
    assert v.stats("grid") == {"accepted": 2, "rejected": 0, "held": 1, "consecutive_errors": 0,
                               "clock_offset": None}


def test_slightly_wrong_accepted_value_recovers_in_two_readings(v):
    """A subtly high counter slips through; the next two real readings restore the baseline."""
    v.validate(grid(imp=42001.0), "grid")
    assert v.validate(grid(imp=42001.25, at=T0 + 10), "grid")[0]        # wrong but plausible → accepted
    ok, msg = v.validate(grid(imp=42001.05, at=T0 + 20), "grid")       # real: "decreased" → held
    assert not ok and "decreased" in msg
    assert v.validate(grid(imp=42001.06, at=T0 + 30), "grid")[0]        # confirms the held reading
    assert v.validate(grid(imp=42001.07, at=T0 + 40), "grid")[0]


def test_alternating_garbage_never_confirms(v):
    v.validate(grid(imp=42001.0), "grid")
    for i, bad in enumerate([1000.0, 99000.0, 5.0, 77777.0], start=1):
        ok, msg = v.validate(grid(imp=bad, at=T0 + 10 * i), "grid")
        assert not ok and msg.startswith("held:")
    assert v.validate(grid(imp=42001.1, at=T0 + 60), "grid")[0]


def test_persistent_regime_change_confirmed_after_two_readings(v, caplog):
    v.validate(grid(imp=42001.0), "grid")
    with caplog.at_level(logging.WARNING):
        assert not v.validate(grid(imp=41000.0, at=T0 + 10), "grid")[0]
        assert v.validate(grid(imp=41000.01, at=T0 + 20), "grid")[0]
    assert "re-baselined after confirmation" in caplog.text
    assert v.validate(grid(imp=41000.02, at=T0 + 30), "grid")[0]


def test_forced_rebaseline_after_max_consecutive_errors(v, caplog):
    """Backstop: readings that never confirm each other still get through eventually."""
    v.validate(grid(imp=42001.0), "grid")
    n = ValidationConfig().max_consecutive_errors
    with caplog.at_level(logging.WARNING):
        for i in range(1, n):
            assert not v.validate(grid(imp=30000.0 + i * 1000, at=T0 + 10 * i), "grid")[0]
        assert v.validate(grid(imp=30000.0 + n * 1000, at=T0 + 10 * n), "grid")[0]
    assert "consecutive rejections" in caplog.text


# --- power jump confirmation ----------------------------------------------

def test_power_jump_held_then_confirmed(v):
    v.validate(grid(power=300.0), "grid")
    ok, msg = v.validate(grid(power=11000.0, imp=42001.02, at=T0 + 10), "grid")   # charger starts
    assert not ok and "power jump" in msg
    assert v.validate(grid(power=10900.0, imp=42001.05, at=T0 + 20), "grid")[0]   # confirmed
    assert v.stats("grid")["held"] == 1 and v.stats("grid")["rejected"] == 0


def test_single_garbage_power_never_accepted(v):
    v.validate(grid(power=300.0), "grid")
    assert not v.validate(grid(power=25000.0, imp=42001.01, at=T0 + 10), "grid")[0]
    assert v.validate(grid(power=310.0, imp=42001.02, at=T0 + 20), "grid")[0]
    assert v.get_data_quality("grid") == 1.0   # held readings do not count as rejections


def test_small_power_changes_pass_immediately(v):
    v.validate(grid(power=300.0), "grid")
    assert v.validate(grid(power=2500.0, imp=42001.01, at=T0 + 10), "grid")[0]


def test_explained_jump_passes_immediately(v):
    v.validate(grid(power=300.0), "grid")
    assert v.validate(grid(power=11300.0, imp=42001.02, at=T0 + 10), "grid", expected_jump=11000.0)[0]
    # and back down when the car stops
    assert v.validate(grid(power=250.0, imp=42001.05, at=T0 + 20), "grid", expected_jump=-11000.0)[0]


def test_jump_beyond_explanation_is_still_held(v):
    v.validate(grid(power=300.0), "grid")
    ok, msg = v.validate(grid(power=25000.0, imp=42001.02, at=T0 + 10), "grid", expected_jump=11000.0)
    assert not ok and "charger explains 11000 W" in msg


def test_power_jump_confirm_can_be_disabled():
    v = SensorValidator(ValidationConfig(power_jump_confirm=0))
    v.validate(grid(power=300.0), "grid")
    assert v.validate(grid(power=25000.0, imp=42001.01, at=T0 + 10), "grid")[0]


def test_bounds_failures_never_rebaseline(v):
    v.validate(grid(), "grid")
    for i in range(1, 30):
        assert not v.validate(grid(power=50000, at=T0 + 10 * i), "grid")[0]


# --- quality --------------------------------------------------------------

def test_quality_window_bounded(v):
    for i in range(100):
        v.validate(grid(imp=42001.0 + i * 0.01, at=T0 + 10 * i), "grid")
    assert v.get_data_quality("grid") == 1.0
    for i in range(10):
        v.validate(grid(power=50000, at=T0 + 2000 + 10 * i), "grid")
    assert v.get_data_quality("grid") == 0.5
    assert v.stats("grid")["accepted"] == 100
    assert v.stats("grid")["rejected"] == 10


def test_unknown_source_quality_is_one(v):
    assert v.get_data_quality("nope") == 1.0


# --- clock ----------------------------------------------------------------

def test_device_time_never_rejects_but_is_not_trusted_when_off(v, caplog):
    now = time.time()
    behind = datetime.fromtimestamp(now - 3600).isoformat(timespec="seconds")
    with caplog.at_level(logging.WARNING):
        ok, _ = v.validate(grid(at=now, ts=behind), "grid")
    assert ok
    assert not v.device_time_trusted("grid")
    assert "Timezone" in caplog.text
    assert 3599 < v.clock_offset("grid") < 3601


def test_device_time_trusted_when_in_sync(v):
    now = time.time()
    ts = datetime.fromtimestamp(now - 5).isoformat(timespec="seconds")
    v.validate(grid(at=now, ts=ts), "grid")
    assert v.device_time_trusted("grid")


def test_missing_timestamp_not_trusted(v):
    v.validate(grid(), "grid")
    assert not v.device_time_trusted("grid")
    assert v.clock_offset("grid") is None


# --- non-finite values -------------------------------------------------------

def test_nan_and_inf_are_rejected(v):
    nan, inf = float("nan"), float("inf")
    for r in (grid(power=nan), grid(imp=nan), grid(exp=inf), pv(power=inf)):
        ok, msg = v.validate(r, r.source)
        assert not ok and "non-finite" in msg
    assert v.stats("grid")["rejected"] == 3 and v.stats("grid")["accepted"] == 0


# --- explanation is signed, jump check skipped across long gaps -------------

def test_charger_stop_does_not_excuse_a_grid_rise(v):
    v.validate(grid(power=300.0), "grid")
    ok, msg = v.validate(grid(power=11300.0, imp=42001.02, at=T0 + 10), "grid", expected_jump=-11000.0)
    assert not ok and "power jump" in msg


def test_no_jump_check_after_a_long_gap(v):
    v.validate(grid(power=300.0), "grid")
    assert v.validate(grid(power=4000.0, imp=42002.0, at=T0 + 600), "grid")[0]


def test_jump_check_gap_can_be_disabled():
    v = SensorValidator(ValidationConfig(power_jump_max_gap=0))
    v.validate(grid(power=300.0), "grid")
    assert not v.validate(grid(power=4000.0, imp=42002.0, at=T0 + 600), "grid")[0]


def test_device_time_not_trusted_beyond_alignment_skew(v):
    # 45 s offset: below clock_offset_warn (60) but above max_alignment_skew (30)
    ts = datetime.fromtimestamp(T0 - 45).isoformat(timespec="seconds")
    assert v.validate(grid(ts=ts), "grid")[0]
    assert v.clock_offset("grid") == 45.0 and not v.device_time_trusted("grid")
