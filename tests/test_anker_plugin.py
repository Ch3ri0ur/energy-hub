import asyncio
import time

import pytest

from energy_hub.anker_plugin import AnkerPlugin, _iso_ts, _refresh_live_updates, _SnapshotBuilder
from energy_hub.config import AnkerConfig
from tests.fake_mqtt import FakeClient


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


# --- session watchdog --------------------------------------------------------

class FakeSession:
    def __init__(self, connected=True):
        self.connected = connected
        self.triggers = 0

    def is_connected(self):
        return self.connected

    def realtime_trigger(self, **_):
        self.triggers += 1

    def status_request(self, **_):
        self.triggers += 1


def run_refresh(session, cfg, builder=None, timeout=2.0):
    async def main():
        await asyncio.wait_for(
            _refresh_live_updates(session, {}, {}, cfg, builder, interval=0.01, initial_delay=0, disconnect_grace=0.05),
            timeout=timeout,
        )
    return asyncio.run(main())


def test_refresh_raises_when_session_stays_disconnected():
    with pytest.raises(ConnectionError, match="disconnected"):
        run_refresh(FakeSession(connected=False), AnkerConfig())


def test_refresh_raises_when_no_device_message_arrives():
    cfg = AnkerConfig()
    cfg.data_timeout = 0.05
    with pytest.raises(ConnectionError, match="no Anker device message"):
        run_refresh(FakeSession(), cfg, builder())


def test_refresh_keeps_running_while_data_flows():
    cfg = AnkerConfig()
    cfg.data_timeout = 0.2
    b = builder()
    session = FakeSession()

    async def main():
        task = asyncio.create_task(
            _refresh_live_updates(session, {}, {}, cfg, b, interval=0.01, initial_delay=0, disconnect_grace=0.05)
        )
        for _ in range(6):
            await asyncio.sleep(0.05)
            b.update("S1", {}, {"battery_soc": 55})   # device keeps talking
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert session.triggers > 0


def test_data_timeout_disabled_and_message_marks_flow():
    b = builder()
    before = b.last_message_at
    time.sleep(0.01)
    b.update("M1", {}, {})            # known device, no values: still counts as data flow
    assert b.last_message_at > before
    cfg = AnkerConfig()
    cfg.data_timeout = 0
    session = FakeSession()

    async def main():
        task = asyncio.create_task(_refresh_live_updates(session, {}, {}, cfg, b, interval=0.01, initial_delay=0))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
    asyncio.run(main())


def test_backoff_resets_when_session_connects():
    plugin = AnkerPlugin(AnkerConfig(user="u", password="p"), FakeClient(), "anker/x")
    plugin._backoff = AnkerPlugin.BACKOFF_MAX
    plugin._set_alive(True)
    assert plugin._backoff == AnkerPlugin.BACKOFF_MIN and plugin.is_alive()
    plugin._set_alive(False)
    assert not plugin.is_alive()
