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


# --- failover ---------------------------------------------------------------

def anker(power, at, imp=512.0, exp=100.0):
    return AnkerGridReading(net_grid_power=power, grid_import_energy=imp, grid_export_energy=exp, received_at=at)


def test_energy_counters_stay_on_tasmota_during_failover():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 600))
    e.update_grid_anker(anker(250.0, now))
    s = e.snapshot()["grid"]
    assert s["source"] == "anker" and s["power"] == 250.0
    assert s["energy_import"] == 42001.0 and s["energy_source"] == "tasmota" and s["energy_stale"] is True


def test_energy_counters_from_anker_only_without_tasmota():
    e = Engine(AppConfig())
    e.update_grid_anker(anker(250.0, time.time()))
    s = e.snapshot()["grid"]
    assert s["energy_import"] == 512.0 and s["energy_source"] == "anker" and s["energy_stale"] is False


def test_stale_anker_used_when_tasmota_never_reported():
    e = Engine(AppConfig())
    e.update_grid_anker(anker(250.0, time.time() - 350))  # beyond anker stale_timeout (300)
    s = e.snapshot()["grid"]
    # still attributed to anker (was: source "" and silently zero); with the default
    # stale policy anker's grace deadline equals its stale timeout, so power is zeroed
    assert s["source"] == "anker" and s["stale"] is True and s["power"] == 0.0


def test_both_stale_newer_wins():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 250))
    e.update_grid_anker(anker(250.0, now - 400))
    assert e.snapshot()["grid"]["source"] == "tasmota"
    e2 = Engine(AppConfig())
    e2.update_grid_tasmota(grid(500.0, now - 400))
    e2.update_grid_anker(anker(250.0, now - 250))
    assert e2.snapshot()["grid"]["source"] == "anker"


def test_failback_hysteresis():
    cfg = AppConfig()
    cfg.sources.tasmota_grid.failback_after = 60
    e = Engine(cfg)
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 300))
    e.update_grid_anker(anker(250.0, now - 100))
    assert e.snapshot()["grid"]["source"] == "anker"
    # Tasmota comes back: first reading after a >120 s gap starts a new run
    e.update_grid_tasmota(grid(510.0, now - 90, imp=42001.5))
    e.update_grid_anker(anker(255.0, now - 90))
    assert e.snapshot()["grid"]["source"] == "anker"      # run is 0 s old → hold
    e.update_grid_tasmota(grid(520.0, now - 40, imp=42001.6))
    e.update_grid_anker(anker(260.0, now - 40))
    assert e.snapshot()["grid"]["source"] == "anker"      # run is 50 s → still hold
    e.update_grid_tasmota(grid(530.0, now - 20, imp=42001.7))
    e.update_grid_anker(anker(265.0, now - 20))
    assert e.snapshot()["grid"]["source"] == "tasmota"    # run is 70 s → fail back


def test_failback_immediate_when_anker_dies():
    cfg = AppConfig()
    cfg.sources.tasmota_grid.failback_after = 60
    e = Engine(cfg)
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 300))
    e.update_grid_anker(anker(250.0, now - 100))
    assert e.snapshot()["grid"]["source"] == "anker"
    e.update_grid_tasmota(grid(510.0, now - 10, imp=42001.5))
    e.update_grid_anker(anker(255.0, now - 400))          # anker stale now → no reason to hold
    assert e.snapshot()["grid"]["source"] == "tasmota"


# --- dead flags ---------------------------------------------------------------

def test_dead_flags():
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now - 400))          # beyond zero_after (300)
    e.update_pv(pv(1000.0, now - 200))                     # stale (>120) but not dead
    s = e.snapshot()
    assert s["grid"]["stale"] and s["grid"]["dead"] and s["grid"]["power"] == 0.0
    assert s["pv_roof"]["stale"] and not s["pv_roof"]["dead"] and s["pv_roof"]["power"] == 1000.0
    assert s["pv_total"]["stale"] and s["pv_total"]["dead"] is False   # roof still delivering
    assert s["battery"]["dead"] and s["charger"]["dead"]
    assert s["pv_balcony"]["dead"]
    assert s["house"]["dead"]


# --- stale policy / locking ---------------------------------------------------

def test_dead_deadline_never_below_stale_timeout():
    cfg = AppConfig()
    cfg.stale_policy.zero_after = 60
    e = Engine(cfg)
    assert e._stale_grace_deadline(120) == 120
    cfg.stale_policy.zero_after = 300
    assert e._stale_grace_deadline(120) == 300


def test_concurrent_updates_and_snapshots():
    import threading

    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(grid(500.0, now))
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                e.update_charger(float(i % 5000))
                e.update_solarbank(SolarbankReading(output_power=float(i), received_at=time.time()))
                i += 1
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(300):
        s = e.snapshot()
        assert s["charger"]["raw_power"] == 0.0 or not s["charger"]["stale"]
    stop.set()
    t.join()
    assert not errors


# --- alignment must never resurrect a pre-gap sample ------------------------

def test_alignment_lookback_is_bounded_for_pv():
    """A gap in one source must not make the snapshot publish its pre-gap sample
    (and flag the live source stale/dead) just because another source's sample
    is a few seconds older than the newest PV reading."""
    e = Engine(AppConfig())
    now = time.time()
    e.update_pv(pv(1000.0, now - 402, imp=60001.0))
    e.update_pv(pv(1500.0, now - 2, imp=60001.2))
    e.update_grid_tasmota(grid(300.0, now - 8))
    s = e.snapshot()
    assert s["pv_roof"] == {"power": 1500.0, "energy": 60001.2, "stale": False, "dead": False}
    assert s["pv_total"]["dead"] is False


def test_anker_grid_is_not_lagged_one_interval_by_solarbank():
    """The meter updates every ~60 s, the solarbank every 5 s: the previous
    meter sample must not be chosen whenever the solarbank sample is older."""
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_anker(anker(100.0, now - 63))
    e.update_grid_anker(anker(900.0, now - 3))
    e.update_solarbank(SolarbankReading(output_power=50.0, received_at=now - 5))
    s = e.snapshot()
    assert s["grid"]["power"] == 900.0 and s["grid"]["stale"] is False


def test_charger_alignment_lookback_is_bounded():
    e = Engine(AppConfig())
    now = time.time()
    e._charger_history.append((now - 400, 5000.0))   # before a go-e outage
    e._charger_history.append((now - 2, 0.0))
    e.charger_power, e.charger_received_at = 0.0, now - 2
    e.update_grid_tasmota(grid(300.0, now - 8))
    assert e.snapshot()["charger"]["power"] == 0.0


def test_freshness_flags_follow_latest_reading_not_aligned_sample():
    e = Engine(AppConfig())
    now = time.time()
    for i in range(10):
        e.update_solarbank(SolarbankReading(output_power=100.0 + i, battery_soc=50.0, received_at=now - 45 + i * 5))
    e.update_grid_tasmota(grid(500.0, now - 20))
    s = e.snapshot()
    assert s["pv_balcony"]["output_power"] == 105.0       # aligned sample (now-20)
    assert s["battery"]["stale"] is False and s["battery"]["dead"] is False
