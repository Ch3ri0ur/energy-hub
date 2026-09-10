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


def test_dead_sources_publish_null_on_topics_but_zero_in_aggregated():
    c = FakeClient()
    snap = snapshot()
    snap["grid"] = {"power": 0.0, "import_w": 0.0, "export_w": 0.0, "energy_import": 42001.0, "stale": True, "dead": True}
    snap["battery"] = {"soc": 0.0, "power": 0.0, "state": "unknown", "stale": True, "dead": True}
    snap["pv_total"] = {"power": 0.0, "generation": 0.0, "stale": False, "dead": False}
    Publisher(c, OutputConfig()).publish_snapshot(snap)
    grid = json.loads(c.topics("energy/grid")[0].payload)
    assert grid["power"] is None and grid["import_w"] is None and grid["energy_import"] == 42001.0
    battery = json.loads(c.topics("energy/battery")[0].payload)
    assert battery["soc"] is None and battery["state"] == "unknown"
    assert json.loads(c.topics("energy/pv/total")[0].payload)["power"] == 0.0     # not dead → numeric
    agg = json.loads(c.topics("energy/aggregated")[0].payload)
    assert agg["grid"]["power"] == 0.0 and agg["battery"]["soc"] == 0.0
