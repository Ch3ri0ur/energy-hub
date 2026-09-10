import time

from energy_hub.adapters.base import AnkerGridReading, SensorReading, SolarbankReading
from energy_hub.config import AppConfig
from energy_hub.engine import Engine


def grid(power, at, imp=42001.0, exp=28485.0):
    return SensorReading(power=power, energy_import=imp, energy_export=exp, received_at=at, source="grid")


def pv(power, at, imp=60001.0):
    return SensorReading(power=power, energy_import=imp, energy_export=0.0, received_at=at, source="pv")


def test_snapshot_basic_balance():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now))
    e.update_pv(pv(1000.0, now))
    e.update_charger(200.0)
    s = e.snapshot()
    assert s["grid"]["source"] == "tasmota"
    assert s["grid"]["stale"] is False
    assert s["consumption"] == 1500.0
    assert s["house"]["power"] == 1300.0
    assert s["charger"]["power"] == 200.0


def test_rejected_reading_does_not_replace_state():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now))
    e.update_grid_tasmota(grid(99999.0, now + 10))
    assert e.grid_tasmota.power == 500.0


def test_alignment_skew_guard_uses_latest_solarbank():
    """A lagging grid sample must not drag the solarbank back to an old history entry."""
    e = Engine(AppConfig())
    now = time.time()
    for i in range(50):
        e.update_solarbank(SolarbankReading(output_power=100.0 + i, battery_soc=50.0, received_at=now - 250 + i * 5))
    # grid sample from 100 s ago (older than max_alignment_skew, still within stale timeout)
    e.update_grid_tasmota(grid(500.0, now - 100))
    s = e.snapshot()
    assert s["pv_balcony"]["output_power"] == 149.0


def test_alignment_within_skew_picks_matching_sample():
    e = Engine(AppConfig())
    now = time.time()
    for i in range(10):
        e.update_solarbank(SolarbankReading(output_power=100.0 + i, battery_soc=50.0, received_at=now - 45 + i * 5))
    e.update_grid_tasmota(grid(500.0, now - 20))  # reference = now-20 → solarbank sample at now-20 (i=5)
    s = e.snapshot()
    assert s["pv_balcony"]["output_power"] == 105.0


def test_failover_to_anker_when_tasmota_stale():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 600))
    e.update_grid_anker(AnkerGridReading(net_grid_power=250.0, received_at=now))
    s = e.snapshot()
    assert s["grid"]["source"] == "anker"
    assert s["grid"]["power"] == 250.0


def test_charger_start_explains_grid_jump():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(300.0, now - 20))
    e.update_charger(11000.0)                       # go-e reports the car charging
    e.update_grid_tasmota(grid(11300.0, now - 10, imp=42001.03))
    assert e.grid_tasmota.power == 11300.0          # accepted immediately, not held


def test_unexplained_grid_jump_is_held_once():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(300.0, now - 20))
    e.update_grid_tasmota(grid(11300.0, now - 10, imp=42001.03))
    assert e.grid_tasmota.power == 300.0            # held
    e.update_grid_tasmota(grid(11250.0, now, imp=42001.06))
    assert e.grid_tasmota.power == 11250.0          # confirmed
