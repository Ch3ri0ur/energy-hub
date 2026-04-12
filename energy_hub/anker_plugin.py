"""Anker Solix cloud-to-local MQTT bridge plugin.

Runs in a background daemon thread.  Authenticates with the Anker cloud,
subscribes to the A17X7 smart meter and A17C5 Solarbank 3, merges decoded
values into a combined snapshot, and publishes it to the **local** MQTT broker
(via the shared paho client passed in at construction time).

If cloud auth or connectivity fails the plugin retries with exponential backoff.
The rest of the energy-hub keeps running — this plugin is fully optional.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from api.api import AnkerSolixApi
from api.errors import AnkerSolixError

from .config import AnkerConfig

logger = logging.getLogger("energy_hub.anker")

SMART_METER_MODEL = "A17X7"
SOLARBANK_MODEL = "A17C5"

METER_FIELDS = {
    "grid_to_home_power",
    "pv_to_grid_power",
    "grid_import_energy",
    "grid_export_energy",
}

SOLARBANK_FIELDS = {
    "photovoltaic_power",
    "pv_1_power",
    "pv_2_power",
    "pv_3_power",
    "pv_4_power",
    "battery_soc",
    "battery_power_signed",
    "output_power",
    "ac_output_power_signed",
    "grid_to_battery_power",
    "home_demand",
    "temperature",
    "grid_power_signed",
}


# ---------------------------------------------------------------------------
# Helpers (ported from mqtt_mirror.py)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso_ts(value: int | float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _normalize_int(value: Any) -> int | None:
    if value in (None, "", "--"):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    with contextlib.suppress(ValueError):
        return int(float(text))
    return None


def _calc_net_grid(grid_to_home: Any, pv_to_grid: Any) -> int | float | None:
    if grid_to_home is None and pv_to_grid is None:
        return None
    return (grid_to_home or 0) - (pv_to_grid or 0)


def _battery_energy_wh(capacity_wh: Any, soc: Any) -> int | None:
    cap = _normalize_int(capacity_wh)
    s = _normalize_int(soc)
    if cap is None or s is None:
        return None
    return int(cap * s / 100)


def _prune_empty(data: Any) -> Any:
    if isinstance(data, dict):
        pruned = {
            k: _prune_empty(v)
            for k, v in data.items()
            if v not in (None, "", [], {})
        }
        return {k: v for k, v in pruned.items() if v not in ({}, [])}
    if isinstance(data, list):
        return [_prune_empty(i) for i in data if i not in (None, "", [], {})]
    return data


def _select_device(
    devices: list[dict[str, Any]], model: str, sn: str = "",
) -> dict[str, Any]:
    matches = [
        d for d in devices
        if (d.get("device_pn") or d.get("product_code")) == model
    ]
    if sn:
        matches = [d for d in matches if d.get("device_sn") == sn]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"No {model} device found")
    sns = ", ".join(sorted(str(d.get("device_sn", "")) for d in matches))
    raise LookupError(f"Multiple {model} devices ({sns}); specify serial number")


# ---------------------------------------------------------------------------
# Snapshot builder (simplified from CombinedSnapshotMirror)
# ---------------------------------------------------------------------------


class _SnapshotBuilder:
    """Merge incremental Anker MQTT values into a combined snapshot."""

    def __init__(
        self,
        meter: dict[str, Any],
        solarbank: dict[str, Any],
    ) -> None:
        self._roles: dict[str, str] = {
            str(meter.get("device_sn")): "meter",
            str(solarbank.get("device_sn")): "solarbank",
        }
        self._meta = {
            "meter_sn": str(meter.get("device_sn", "")),
            "solarbank_sn": str(solarbank.get("device_sn", "")),
            "battery_capacity_wh": _normalize_int(
                ((solarbank.get("customized") or {}).get("battery_capacity"))
                or solarbank.get("battery_capacity")
            ),
        }
        self._values: dict[str, dict[str, Any]] = {"meter": {}, "solarbank": {}}
        self._last: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def update(
        self,
        device_sn: str,
        message: Any,
        extracted_values: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Incorporate new values.  Returns a snapshot dict if it changed, else None."""
        role = self._roles.get(device_sn)
        if not role or not extracted_values:
            return None

        fields = METER_FIELDS if role == "meter" else SOLARBANK_FIELDS
        filtered = {k: extracted_values[k] for k in fields if k in extracted_values}
        if not filtered:
            return None

        if isinstance(message, dict):
            ts = (message.get("head") or {}).get("timestamp")
            if ts is not None:
                filtered["last_message"] = _iso_ts(ts) if isinstance(ts, (int, float)) else str(ts)

        with self._lock:
            self._values[role] = self._values.get(role, {}) | filtered
            snap = self._build()
            if not snap or snap == self._last:
                return None
            self._last = snap
            return {"timestamp": _iso_now(), **snap}

    def _build(self) -> dict[str, Any] | None:
        meter = self._values.get("meter") or {}
        sb = self._values.get("solarbank") or {}
        if not meter or not sb:
            return None
        g2h = meter.get("grid_to_home_power")
        p2g = meter.get("pv_to_grid_power")
        return _prune_empty({
            "source": "anker_mqtt",
            "meter_sn": self._meta["meter_sn"],
            "solarbank_sn": self._meta["solarbank_sn"],
            "grid_to_home_power": g2h,
            "pv_to_grid_power": p2g,
            "net_grid_power": _calc_net_grid(g2h, p2g),
            "grid_import_energy": meter.get("grid_import_energy"),
            "grid_export_energy": meter.get("grid_export_energy"),
            "photovoltaic_power": sb.get("photovoltaic_power"),
            "pv_1_power": sb.get("pv_1_power"),
            "pv_2_power": sb.get("pv_2_power"),
            "pv_3_power": sb.get("pv_3_power"),
            "pv_4_power": sb.get("pv_4_power"),
            "battery_capacity_wh": self._meta.get("battery_capacity_wh"),
            "battery_soc": sb.get("battery_soc"),
            "battery_energy_wh": _battery_energy_wh(
                self._meta.get("battery_capacity_wh"), sb.get("battery_soc"),
            ),
            "battery_power_signed": sb.get("battery_power_signed"),
            "output_power": sb.get("output_power"),
            "ac_output_power_signed": sb.get("ac_output_power_signed"),
            "grid_to_battery_power": sb.get("grid_to_battery_power"),
            "home_demand": sb.get("home_demand"),
            "temperature": sb.get("temperature"),
            "meter_updated_at": meter.get("last_message"),
            "solarbank_updated_at": sb.get("last_message"),
        }) or None


# ---------------------------------------------------------------------------
# Async helpers (run inside the worker thread's event loop)
# ---------------------------------------------------------------------------


async def _refresh_live_updates(
    mqtt_session: Any,
    meter: dict[str, Any],
    solarbank: dict[str, Any],
    cfg: AnkerConfig,
) -> None:
    """Periodically renew realtime triggers / status requests."""
    await asyncio.sleep(2)
    next_meter = 0.0
    next_sb = 0.0
    loop = asyncio.get_running_loop()
    sb_supports_status = bool(solarbank.get("mqtt_status_request"))

    while True:
        now = loop.time()
        if cfg.meter_trigger_interval and now >= next_meter:
            mqtt_session.realtime_trigger(
                deviceDict=meter, timeout=cfg.trigger_timeout,
            )
            next_meter = now + cfg.meter_trigger_interval
        if cfg.solarbank_status_interval and now >= next_sb:
            if sb_supports_status:
                mqtt_session.status_request(deviceDict=solarbank)
            else:
                mqtt_session.realtime_trigger(
                    deviceDict=solarbank, timeout=cfg.trigger_timeout,
                )
            next_sb = now + cfg.solarbank_status_interval
        await asyncio.sleep(1)


async def _run_bridge(
    cfg: AnkerConfig,
    local_publish,
    stop_event: asyncio.Event,
) -> None:
    """One attempt at Anker auth + MQTT session + poller loop."""
    async with ClientSession() as ws:
        api = AnkerSolixApi(cfg.user, cfg.password, cfg.country, ws, logger)
        if await api.async_authenticate():
            logger.info("Anker cloud auth: OK")
        else:
            logger.info("Anker cloud auth: cached")

        await api.update_sites()
        await api.get_bind_devices()
        devices = list(api.devices.values())

        meter = _select_device(devices, SMART_METER_MODEL, cfg.meter_sn)
        solarbank = _select_device(devices, SOLARBANK_MODEL, cfg.solarbank_sn)
        logger.info(
            "Using meter %s, solarbank %s",
            meter.get("device_sn"), solarbank.get("device_sn"),
        )

        builder = _SnapshotBuilder(meter, solarbank)

        def _on_msg(_session, _topic, message, _data, _model, device_sn, values):
            snap = builder.update(device_sn, message, values)
            if snap:
                local_publish(snap)

        mqtt_session = await api.startMqttSession()
        if not (mqtt_session and mqtt_session.is_connected()):
            raise ConnectionError("Anker MQTT session failed to connect")

        topics: set[str] = set()
        for dev in (meter, solarbank):
            if prefix := mqtt_session.get_topic_prefix(deviceDict=dev):
                topics.add(f"{prefix}#")
        if not topics:
            raise LookupError("No MQTT topics derived for selected devices")

        poller = asyncio.create_task(
            mqtt_session.message_poller(
                topics=topics,
                trigger_devices=set(),
                msg_callback=_on_msg,
                timeout=cfg.trigger_timeout,
            )
        )
        refresh = asyncio.create_task(
            _refresh_live_updates(mqtt_session, meter, solarbank, cfg)
        )
        stop_task = asyncio.create_task(stop_event.wait())

        try:
            done, _ = await asyncio.wait(
                {poller, refresh, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                return  # clean shutdown
            # If poller or refresh exited, let the outer retry loop handle it
            for t in done:
                t.result()  # re-raises exceptions
        finally:
            for t in (stop_task, refresh, poller):
                if not t.done():
                    t.cancel()
                with contextlib.suppress(BaseException):
                    await t
            api.stopMqttSession()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class AnkerPlugin:
    """Resilient Anker cloud bridge running in a daemon thread."""

    BACKOFF_MIN = 30
    BACKOFF_MAX = 300

    def __init__(
        self,
        cfg: AnkerConfig,
        local_mqtt_client,          # paho.mqtt.client.Client (thread-safe publish)
        output_topic: str,          # e.g. "anker/solix/live/system"
    ) -> None:
        self._cfg = cfg
        self._mqtt = local_mqtt_client
        self._topic = output_topic
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._alive = False
        self._last_error: str = ""
        self._retry_count = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._cfg.enabled:
            logger.info("Anker plugin disabled (no credentials)")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_thread,
            name="anker-plugin",
            daemon=True,
        )
        self._thread.start()
        logger.info("Anker plugin thread started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Anker plugin stopped")

    def is_alive(self) -> bool:
        return self._alive

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def retry_count(self) -> int:
        return self._retry_count

    # -- local MQTT publish --------------------------------------------------

    def _publish_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Publish combined Anker snapshot to the local broker."""
        try:
            payload = json.dumps(snapshot, separators=(",", ":"))
            self._mqtt.publish(self._topic, payload=payload, qos=0, retain=True)
        except Exception:
            logger.exception("Failed to publish Anker snapshot to local MQTT")

    # -- thread entry --------------------------------------------------------

    def _run_thread(self) -> None:
        """Background thread: retry loop with exponential backoff."""
        backoff = self.BACKOFF_MIN
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_async = asyncio.Event()

        # Bridge _stop (threading.Event) → stop_async (asyncio.Event)
        def _watch_stop():
            self._stop.wait()
            loop.call_soon_threadsafe(stop_async.set)

        watcher = threading.Thread(target=_watch_stop, daemon=True)
        watcher.start()

        try:
            while not self._stop.is_set():
                try:
                    self._alive = True
                    self._last_error = ""
                    loop.run_until_complete(
                        _run_bridge(self._cfg, self._publish_snapshot, stop_async)
                    )
                    if self._stop.is_set():
                        break
                    # Clean exit from bridge — shouldn't happen, treat as error
                    self._last_error = "Bridge exited unexpectedly"
                    logger.warning("Anker bridge exited, retrying in %ds", backoff)
                except (
                    ClientError,
                    TimeoutError,
                    OSError,
                    AnkerSolixError,
                    LookupError,
                    ConnectionError,
                    RuntimeError,
                ) as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.error("Anker plugin error: %s, retrying in %ds", exc, backoff)
                except Exception:
                    self._last_error = "unexpected error"
                    logger.exception("Anker plugin unexpected error, retrying in %ds", backoff)

                self._alive = False
                self._retry_count += 1

                # Wait with backoff, but respect stop signal
                if self._stop.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, self.BACKOFF_MAX)
        finally:
            self._alive = False
            loop.close()
