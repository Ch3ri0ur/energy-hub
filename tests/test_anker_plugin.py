from energy_hub.anker_plugin import _iso_ts, _SnapshotBuilder


def builder():
    return _SnapshotBuilder({"device_sn": "M1"}, {"device_sn": "S1", "battery_capacity": 2688})


def test_meter_only_snapshot_is_published():
    b = builder()
    snap = b.update("M1", {"head": {"timestamp": 1_800_000_000}}, {"grid_to_home_power": 100, "pv_to_grid_power": 0})
    assert snap is not None
    assert snap["net_grid_power"] == 100 and "battery_soc" not in snap
    assert snap["meter_updated_at"].startswith("2027-")


def test_solarbank_only_snapshot_is_published():
    snap = builder().update("S1", {}, {"battery_soc": 55, "output_power": 300})
    assert snap["battery_soc"] == 55 and snap["battery_energy_wh"] == 1478 and "net_grid_power" not in snap


def test_unchanged_values_do_not_republish():
    b = builder()
    assert b.update("S1", {}, {"battery_soc": 55}) is not None
    assert b.update("S1", {}, {"battery_soc": 55}) is None


def test_unknown_device_or_fields_ignored():
    b = builder()
    assert b.update("X", {}, {"battery_soc": 1}) is None
    assert b.update("S1", {}, {"unrelated": 1}) is None


def test_iso_ts_seconds_millis_garbage():
    assert _iso_ts(1_700_000_000) == "2023-11-14T22:13:20Z"
    assert _iso_ts(1_700_000_000_000) == "2023-11-14T22:13:20Z"
    assert _iso_ts(float("inf")) == "inf"
