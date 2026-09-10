import json

import paho.mqtt.client as mqtt

from energy_hub.config import AppConfig
from energy_hub.main import _build_router
from tests.fake_mqtt import FakeMessage


class RecordingEngine:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("update_"):
            return lambda *a: self.calls.append((name, a))
        raise AttributeError(name)


class NoGas:
    topics = []

    def on_message(self, *_):
        raise AssertionError("not expected")


def make():
    cfg = AppConfig()
    cfg.sources.tasmota_pv.topic = "tele/+/SENSOR"
    cfg.sources.tasmota_grid.topic = ""
    cfg.sources.anker.topic = ""
    cfg.sources.charger.topic = "go-eCharger/1/nrg"
    eng = RecordingEngine()
    return _build_router(cfg, eng, NoGas()), eng


def test_routes_only_configured_topics():
    routes, _ = make()
    assert set(routes) == {"tele/+/SENSOR", "go-eCharger/1/nrg"}


def test_wildcard_topic_matches_with_paho_semantics():
    routes, _ = make()
    assert mqtt.topic_matches_sub("tele/+/SENSOR", "tele/tasmota_pv/SENSOR")


def test_charger_live_message_routed_and_retained_ignored():
    routes, eng = make()
    nrg = json.dumps([0] * 11 + [3700] + [0] * 4).encode()
    routes["go-eCharger/1/nrg"](None, None, FakeMessage("go-eCharger/1/nrg", nrg, retain=True))
    assert eng.calls == []
    routes["go-eCharger/1/nrg"](None, None, FakeMessage("go-eCharger/1/nrg", nrg))
    assert eng.calls == [("update_charger", (3700.0,))]


def test_pv_route_parses_tasmota():
    routes, eng = make()
    payload = b'{"Time":"2026-04-12T14:30:00","SML":{"Total_in":65432.1,"Total_out":0.0,"Power_curr":1234.5}}'
    routes["tele/+/SENSOR"](None, None, FakeMessage("tele/tasmota_pv/SENSOR", payload))
    (name, (reading,)), = eng.calls
    assert name == "update_pv" and reading.power == 1234.5 and reading.source == "pv"


# --- robustness: a handler error must never escape into paho's network thread


class ExplodingEngine:
    def __getattr__(self, name):
        if name.startswith("update_"):
            def boom(*_):
                raise RuntimeError("engine broke")
            return boom
        raise AttributeError(name)


def test_handler_exception_is_swallowed_and_logged(caplog):
    cfg = AppConfig()
    cfg.sources.charger.topic = "go-eCharger/1/nrg"
    routes = _build_router(cfg, ExplodingEngine(), NoGas())
    nrg = json.dumps([0] * 11 + [3700] + [0] * 4).encode()
    with caplog.at_level("ERROR"):
        routes["go-eCharger/1/nrg"](None, None, FakeMessage("go-eCharger/1/nrg", nrg))
    assert "engine broke" in caplog.text


def test_gas_route_is_guarded_too():
    class BrokenGas:
        topics = ["zigbee2mqtt/x"]

        def on_message(self, *_):
            raise ValueError("gas broke")

    cfg = AppConfig()
    routes = _build_router(cfg, RecordingEngine(), BrokenGas())
    routes["zigbee2mqtt/x"](None, None, FakeMessage("zigbee2mqtt/x", b"{}"))  # must not raise


def test_scalar_anker_payload_does_not_raise():
    cfg = AppConfig()
    cfg.sources.anker.topic = "anker/x"
    eng = RecordingEngine()
    routes = _build_router(cfg, eng, NoGas())
    for payload in (b"42", b"null", b"true", b'"str"', b"[1,2]"):
        routes["anker/x"](None, None, FakeMessage("anker/x", payload))
    assert eng.calls == []
