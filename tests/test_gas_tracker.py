import json
import logging

import pytest

from energy_hub.config import GasConfig
from energy_hub.gas_tracker import GasTracker
from tests.fake_mqtt import FakeClient, FakeMessage

IMP = "zigbee2mqtt/GasImpulsCounter"
SYNC = "energy/gas/set"
PUB = "energy/gas"


@pytest.fixture
def tracker(tmp_path):
    client = FakeClient()
    cfg = GasConfig(state_file=str(tmp_path / "gas_state.json"))
    return GasTracker(client, cfg), client


def contact(tracker, value, retained=False, **extra):
    t, _ = tracker
    t.on_message(None, None, FakeMessage(IMP, json.dumps({"contact": value, **extra}).encode(), retain=retained))


def last_payload(client):
    return json.loads(client.topics(PUB)[-1].payload)


# --- counting ---------------------------------------------------------------

def test_first_message_true_is_not_counted(tracker):
    t, c = tracker
    contact(tracker, True)
    assert t._impulse_count == 0
    assert c.topics(PUB) == []


def test_retained_true_then_false_true_counts_once(tracker):
    t, c = tracker
    contact(tracker, True, retained=True)
    contact(tracker, False)
    contact(tracker, True)
    assert t._impulse_count == 1
    assert last_payload(c)["volume_m3"] == 0.01


def test_rising_edges_only(tracker):
    t, _ = tracker
    for v in (False, True, True, False, True):
        contact(tracker, v)
    assert t._impulse_count == 2


def test_retained_message_mid_run_never_counts(tracker):
    t, _ = tracker
    contact(tracker, False)
    contact(tracker, True, retained=True)
    assert t._impulse_count == 0


# --- sync -------------------------------------------------------------------

def sync(tracker, payload: bytes):
    t, _ = tracker
    t.on_message(None, None, FakeMessage(SYNC, payload))


def test_sync_documented_key(tracker):
    t, c = tracker
    sync(tracker, b'{"volume_m3": 100.5}')
    assert t._impulse_count == 10050
    assert last_payload(c)["volume_m3"] == 100.5
    # retained sync message is cleared afterwards
    clear = c.topics(SYNC)[-1]
    assert clear.payload is None and clear.retain is True


def test_sync_legacy_key(tracker):
    t, _ = tracker
    sync(tracker, b'{"m3": 100.5}')
    assert t._impulse_count == 10050


def test_sync_negative_and_garbage_ignored(tracker, caplog):
    t, c = tracker
    sync(tracker, b'{"volume_m3": 100.5}')
    with caplog.at_level(logging.WARNING):
        sync(tracker, b'{"volume_m3": -1}')
        sync(tracker, b'not json')
        sync(tracker, b'{"foo": 1}')
        sync(tracker, b'')          # our own retained-clear echo: silent
    assert t._impulse_count == 10050
    assert caplog.text.count("GasTracker") == 3


# --- persistence --------------------------------------------------------------

def test_state_round_trip_atomic(tmp_path):
    cfg = GasConfig(state_file=str(tmp_path / "s.json"))
    t = GasTracker(FakeClient(), cfg)
    t.on_message(None, None, FakeMessage(SYNC, b'{"volume_m3": 12.34}'))
    assert not (tmp_path / "s.json.tmp").exists()
    assert json.loads((tmp_path / "s.json").read_text())["impulse_count"] == 1234
    assert GasTracker(FakeClient(), cfg)._impulse_count == 1234


def test_corrupt_state_logs_error_with_resync_hint(tmp_path, caplog):
    path = tmp_path / "s.json"
    path.write_text('{"impulse_co')
    with caplog.at_level(logging.ERROR):
        t = GasTracker(FakeClient(), GasConfig(state_file=str(path)))
    assert t._impulse_count == 0
    assert "re-sync" in caplog.text


# --- publishing ---------------------------------------------------------------

def test_publish_on_demand_includes_battery(tracker):
    t, c = tracker
    contact(tracker, False, battery=100, battery_low=False)
    t.publish()
    p = last_payload(c)
    assert p["battery"] == 100 and p["battery_low"] is False and p["impulse_count"] == 0


def test_battery_change_publishes_without_impulse(tracker):
    t, c = tracker
    contact(tracker, False, battery=100)
    n = len(c.topics(PUB))
    contact(tracker, False, battery=90)
    assert len(c.topics(PUB)) == n + 1
    assert last_payload(c)["battery"] == 90
    contact(tracker, False, battery=90)
    assert len(c.topics(PUB)) == n + 1


def test_non_utf8_and_non_object_payloads_ignored(tracker):
    t, client = tracker
    for raw in (b"\xff\xfe", b"42", b"[1]", b"not json"):
        t.on_message(None, None, FakeMessage(IMP, raw))
        t.on_message(None, None, FakeMessage(SYNC, raw))
    assert t._impulse_count == 0
