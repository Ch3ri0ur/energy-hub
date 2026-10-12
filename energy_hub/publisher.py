"""MQTT output publisher — publishes aggregated data to well-structured topics."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import paho.mqtt.client as mqtt

from .config import OutputConfig

logger = logging.getLogger("energy_hub.publisher")


# Fields that are set to null on the per-topic (evcc-facing) payloads when the
# source is dead.  evcc reads these via jq; a null makes the meter fail loudly,
# whereas the 0 kept in the aggregated blob (for Telegraf) would be taken as a
# real reading and used for charging decisions.
_NULL_WHEN_DEAD: dict[str, tuple[str, ...]] = {
    "grid": ("power", "import_w", "export_w"),
    "pv_roof": ("power",),
    "pv_balcony": ("power", "output_power", "pv_1_power", "pv_2_power", "pv_3_power", "pv_4_power"),
    "pv_total": ("power", "generation"),
    "battery": ("soc", "power", "charge_w", "discharge_w", "ac_charge_w", "energy_wh"),
    "charger": ("power", "raw_power"),
    "house": ("power",),
}


def _for_topic(section: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Copy of a snapshot section with dead-source values replaced by null."""
    if not section or not section.get("dead"):
        return section
    out = dict(section)
    for field in _NULL_WHEN_DEAD.get(key, ()):
        out[field] = None
    return out


class Publisher:
    """Publish engine snapshots to local MQTT as individual + aggregated topics."""

    # QoS 1 for the topics Telegraf ingests, so its persistent session queues
    # them while it is down; the evcc-facing topics stay at QoS 0 (latest wins).
    TELEGRAF_QOS = 1

    def __init__(self, client: mqtt.Client, cfg: OutputConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._prefix = cfg.prefix.rstrip("/")
        self._last_publish = 0.0
        self._failing = False   # log the first failed publish and the recovery, not every tick

    @property
    def health_topic(self) -> str:
        return f"{self._prefix}/health"

    @staticmethod
    def offline_payload(timestamp: float | None = None) -> str:
        """Retained health payload used as MQTT Last Will and on clean shutdown.

        The Last Will is registered at connect time, so it cannot carry the time
        of death: the key is omitted rather than set to null, which Telegraf's
        json_v2 parser rejects ("timestamp path returned NULL").  The Telegraf
        health consumer therefore uses the arrival time, see docs/telegraf-energy.conf.
        """
        payload: dict[str, Any] = {
            "status": "offline",
            "connected": False,
            "anker_plugin_alive": False,
            "issues": ["hub_offline"],
            "sources": {},
        }
        if timestamp is not None:
            payload["timestamp"] = timestamp
        return json.dumps(payload, separators=(",", ":"))

    def publish_offline(self) -> None:
        try:
            info = self._client.publish(self.health_topic, self.offline_payload(time.time()), qos=0, retain=True)
            info.wait_for_publish(timeout=1.0)
        except (RuntimeError, ValueError) as exc:   # not connected: the Last Will covers it
            logger.debug("Offline health not published (%s)", exc)
        except Exception:
            logger.exception("Failed to publish offline health")

    def should_publish(self) -> bool:
        return (time.time() - self._last_publish) >= self._cfg.publish_interval

    def publish_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Publish individual topics + the full aggregated blob."""
        prefix = self._prefix
        retain = self._cfg.retain

        # Individual topics (evcc and friends): null instead of 0 for dead sources
        self._pub(f"{prefix}/grid", _for_topic(snapshot.get("grid"), "grid"), retain)
        self._pub(f"{prefix}/pv/roof", _for_topic(snapshot.get("pv_roof"), "pv_roof"), retain)
        self._pub(f"{prefix}/pv/balcony", _for_topic(snapshot.get("pv_balcony"), "pv_balcony"), retain)
        self._pub(f"{prefix}/pv/total", _for_topic(snapshot.get("pv_total"), "pv_total"), retain)
        self._pub(f"{prefix}/battery", _for_topic(snapshot.get("battery"), "battery"), retain)
        self._pub(f"{prefix}/charger", _for_topic(snapshot.get("charger"), "charger"), retain)
        self._pub(f"{prefix}/house", _for_topic(snapshot.get("house"), "house"), retain)

        # Full aggregated blob (for Telegraf / InfluxDB): always numeric
        self._pub(f"{prefix}/aggregated", snapshot, retain, qos=self.TELEGRAF_QOS)

        self._last_publish = time.time()
        logger.debug(
            "Published: consumption=%.0fW grid=%.0fW pv=%.0fW",
            snapshot.get("consumption", 0),
            (snapshot.get("grid") or {}).get("power", 0),
            (snapshot.get("pv_total") or {}).get("power", 0),
        )

    def publish_health(self, health: dict[str, Any]) -> None:
        self._pub(self.health_topic, health, retain=self._cfg.retain, qos=self.TELEGRAF_QOS)

    def publish_alert(self, alert: dict[str, Any]) -> None:
        self._pub(f"{self._prefix}/alerts", alert, retain=False)

    def _pub(self, topic: str, payload: Any, retain: bool, qos: int = 0) -> None:
        if payload is None:
            return
        try:
            info = self._client.publish(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=qos,
                retain=retain,
            )
        except Exception:
            logger.exception("Failed to publish to %s", topic)
            return
        # paho returns MQTT_ERR_NO_CONN etc. silently instead of raising.
        rc = getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            if not self._failing:
                logger.warning("Publish to %s failed (rc=%s); broker connection lost?", topic, rc)
                self._failing = True
        elif self._failing:
            logger.info("Publishing works again")
            self._failing = False
