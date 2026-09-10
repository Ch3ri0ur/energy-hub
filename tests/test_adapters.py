import time
from datetime import UTC, datetime

from energy_hub.adapters.anker_data import parse_anker_snapshot
from energy_hub.adapters.charger import parse_charger_nrg
from energy_hub.adapters.tasmota_sml import parse_tasmota_sml

TASMOTA = b'{"Time":"2026-04-12T14:30:00","SML":{"Total_in":65432.1,"Total_out":0.0,"Power_curr":1234.5}}'


# --- tasmota ------------------------------------------------------------------

def test_tasmota_valid_bytes_and_str():
    for payload in (TASMOTA, TASMOTA.decode()):
        r = parse_tasmota_sml(payload, source="pv")
        assert (r.power, r.energy_import, r.energy_export, r.timestamp, r.source) == (
            1234.5, 65432.1, 0.0, "2026-04-12T14:30:00", "pv")
        assert time.time() - r.received_at < 1


def test_tasmota_missing_or_garbage():
    assert parse_tasmota_sml(b'{"Time":"x"}') is None
    assert parse_tasmota_sml(b'{"SML":{"Total_in":"abc","Total_out":0,"Power_curr":0}}') is None
    assert parse_tasmota_sml(b"not json") is None
    assert parse_tasmota_sml(b"[]") is None


# --- charger --------------------------------------------------------------------

def test_charger_nrg():
    assert parse_charger_nrg(("[" + ",".join(["0"] * 11 + ["3700"] + ["0"] * 4) + "]").encode()) == 3700.0
    assert parse_charger_nrg(b"[1,2,3]") is None
    assert parse_charger_nrg(b"{}") is None
    assert parse_charger_nrg(b"garbage") is None


# --- anker ---------------------------------------------------------------------

def test_anker_meter_only():
    grid, sb = parse_anker_snapshot(b'{"grid_to_home_power": 120, "pv_to_grid_power": 20, "grid_import_energy": 5.5}')
    assert sb is None
    assert grid.net_grid_power == 100.0 and grid.grid_import_energy == 5.5 and grid.grid_export_energy == 0.0


def test_anker_explicit_net_power_wins():
    grid, _ = parse_anker_snapshot(b'{"grid_to_home_power": 120, "pv_to_grid_power": 20, "net_grid_power": 95}')
    assert grid.net_grid_power == 95.0


def test_anker_solarbank_only_and_timestamps():
    ts = "2026-04-12T12:00:00Z"
    grid, sb = parse_anker_snapshot(
        f'{{"battery_soc": 42, "output_power": 300, "solarbank_updated_at": "{ts}"}}'.encode())
    assert grid is None
    assert sb.battery_soc == 42.0 and sb.output_power == 300.0
    assert sb.received_at == datetime(2026, 4, 12, 12, tzinfo=UTC).timestamp()
    assert sb.battery_state == "idle"


def test_anker_battery_state():
    _, sb = parse_anker_snapshot(b'{"battery_soc": 1, "battery_power_signed": 200}')
    assert sb.battery_state == "charging"
    _, sb = parse_anker_snapshot(b'{"battery_soc": 1, "battery_power_signed": -200}')
    assert sb.battery_state == "discharging"


def test_anker_garbage():
    assert parse_anker_snapshot(b"nope") == (None, None)
    assert parse_anker_snapshot(b"{}") == (None, None)
