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
    assert p.health_topic == "energy/health"
