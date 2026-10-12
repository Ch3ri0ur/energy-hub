import json

from energy_hub.config import OutputConfig
from energy_hub.publisher import Publisher
from tests.fake_mqtt import FakeClient


def snapshot():
    return {
        "grid": {"power": 1.0}, "pv_roof": {"power": 2.0}, "pv_balcony": {"power": 3.0},
        "pv_total": {"power": 5.0}, "battery": {"soc": 50.0}, "charger": {"power": 0.0},
        "house": {"power": 6.0}, "consumption": 6.0, "timestamp": 1.0,
    }


def test_snapshot_topics_and_retain():
    c = FakeClient()
    Publisher(c, OutputConfig(prefix="energy/", retain=True)).publish_snapshot(snapshot())
    topics = [p.topic for p in c.published]
    assert topics == ["energy/grid", "energy/pv/roof", "energy/pv/balcony", "energy/pv/total",
                      "energy/battery", "energy/charger", "energy/house", "energy/aggregated"]
    assert all(p.retain for p in c.published)
    assert json.loads(c.topics("energy/aggregated")[0].payload)["consumption"] == 6.0


def test_alerts_never_retained_and_offline_payload():
    c = FakeClient()
    p = Publisher(c, OutputConfig())
    p.publish_alert({"issue": "x"})
    assert c.published[-1].retain is False
    off = json.loads(p.offline_payload())
    assert off["status"] == "offline" and off["connected"] is False and "hub_offline" in off["issues"]
    assert "timestamp" not in off            # Last Will: null would be dropped by Telegraf json_v2
    assert json.loads(p.offline_payload(12.5))["timestamp"] == 12.5
    assert p.health_topic == "energy/health"


def test_dead_sources_publish_null_on_topics_and_are_omitted_in_aggregated():
    c = FakeClient()
    snap = snapshot()
    snap["grid"] = {"power": 0.0, "import_w": 0.0, "export_w": 0.0, "energy_import": 42001.0, "stale": True, "dead": True}
    snap["battery"] = {"soc": 0.0, "power": 0.0, "ac_charge_w": 0.0, "state": "unknown", "stale": True, "dead": True}
    snap["pv_total"] = {"power": 0.0, "generation": 0.0, "stale": False, "dead": False}
    Publisher(c, OutputConfig()).publish_snapshot(snap)
    grid = json.loads(c.topics("energy/grid")[0].payload)
    assert grid["power"] is None and grid["import_w"] is None and grid["energy_import"] == 42001.0
    battery = json.loads(c.topics("energy/battery")[0].payload)
    assert battery["soc"] is None and battery["ac_charge_w"] is None and battery["state"] == "unknown"
    assert json.loads(c.topics("energy/pv/total")[0].payload)["power"] == 0.0     # not dead → numeric
    agg = json.loads(c.topics("energy/aggregated")[0].payload)
    assert "power" not in agg["grid"] and "import_w" not in agg["grid"]
    assert agg["grid"]["energy_import"] == 42001.0 and agg["grid"]["dead"] is True    # frozen counter stays
    assert agg["battery"] == {"stale": True, "dead": True}     # no SoC 0 %, no "unknown" state
    assert agg["pv_total"]["power"] == 0.0 and agg["consumption"] == 6.0   # not dead → kept


def test_aggregated_omits_unknown_counters_and_incomplete_balance():
    c = FakeClient()
    snap = snapshot()
    snap["grid"] = {"power": 500.0, "energy_import": None, "energy_source": "", "dead": False}
    snap["pv_roof"] = {"power": 0.0, "energy": None, "dead": True}
    snap.update(self_sufficiency_pct=40.0, self_consumption_pct=100.0, balance_dead=True)
    Publisher(c, OutputConfig()).publish_snapshot(snap)
    agg = json.loads(c.topics("energy/aggregated")[0].payload)
    assert agg["grid"] == {"power": 500.0, "dead": False}
    assert agg["pv_roof"] == {"dead": True}
    assert not {"consumption", "self_sufficiency_pct", "self_consumption_pct"} & agg.keys()
    assert agg["balance_dead"] is True and agg["timestamp"] == 1.0
    assert agg["house"]["power"] == 6.0                        # house has its own dead flag
    assert "null" not in c.topics("energy/aggregated")[0].payload
    grid = json.loads(c.topics("energy/grid")[0].payload)
    assert grid["energy_import"] is None                       # per-topic: explicit null


def test_telegraf_topics_use_qos1_and_publish_failures_are_logged_once(caplog):
    c = FakeClient()
    p = Publisher(c, OutputConfig())
    p.publish_snapshot(snapshot())
    p.publish_health({"status": "ok"})
    qos = {m.topic: m.qos for m in c.published}
    assert qos["energy/aggregated"] == 1 and qos["energy/health"] == 1 and qos["energy/grid"] == 0
    c.rc = 4   # MQTT_ERR_NO_CONN
    with caplog.at_level("WARNING"):
        p.publish_snapshot(snapshot())
        p.publish_snapshot(snapshot())
    assert caplog.text.count("failed (rc=4)") == 1
    c.rc = 0
    with caplog.at_level("INFO"):
        p.publish_snapshot(snapshot())
    assert "works again" in caplog.text


def test_publish_offline_with_fake_client():
    c = FakeClient()
    Publisher(c, OutputConfig()).publish_offline()
    assert json.loads(c.published[-1].payload)["status"] == "offline"


def test_for_telegraf_keeps_false_and_zero_drops_none_and_empty():
    from energy_hub.publisher import _for_telegraf
    out = _for_telegraf({"grid": {"power": 0.0, "n": 0, "stale": False, "source": "", "energy_import": None},
                         "self_sufficiency_pct": 0.0, "balance_dead": False, "x": None})
    assert out == {"grid": {"power": 0.0, "n": 0, "stale": False}, "self_sufficiency_pct": 0.0, "balance_dead": False}


def test_fresh_engine_publishes_no_fake_values():
    """Hub startup: nothing reported yet → only flags and the timestamp reach Telegraf."""
    from energy_hub.config import AppConfig
    from energy_hub.engine import Engine
    c = FakeClient()
    Publisher(c, OutputConfig()).publish_snapshot(Engine(AppConfig()).snapshot())
    agg = json.loads(c.topics("energy/aggregated")[0].payload)
    assert agg["balance_dead"] is True and "consumption" not in agg
    numbers = [v for sec in agg.values() if isinstance(sec, dict) for v in sec.values()
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert numbers == []
    assert json.loads(c.topics("energy/house")[0].payload)["power"] is None


def test_house_and_pv_total_null_on_topics_when_one_input_is_dead():
    import time

    from energy_hub.adapters.base import SensorReading, SolarbankReading
    from energy_hub.config import AppConfig
    from energy_hub.engine import Engine
    e = Engine(AppConfig())
    now = time.time()
    e.update_grid_tasmota(SensorReading(power=500.0, energy_import=42001.0, energy_export=28485.0, received_at=now, source="grid"))
    e.update_pv(SensorReading(power=1000.0, energy_import=60001.0, energy_export=0.0, received_at=now, source="pv"))
    e.update_charger(0.0)
    e.update_solarbank(SolarbankReading(output_power=300.0, received_at=now - 400))   # Solarbank dead
    c = FakeClient()
    Publisher(c, OutputConfig()).publish_snapshot(e.snapshot())
    assert json.loads(c.topics("energy/house")[0].payload)["power"] is None
    assert json.loads(c.topics("energy/pv/total")[0].payload)["power"] is None
    assert json.loads(c.topics("energy/pv/roof")[0].payload)["power"] == 1000.0
    agg = json.loads(c.topics("energy/aggregated")[0].payload)
    assert agg["pv_roof"]["power"] == 1000.0 and "power" not in agg["house"] and "power" not in agg["pv_total"]
