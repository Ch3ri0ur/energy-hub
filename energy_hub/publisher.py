"""MQTT output publisher — publishes aggregated data to well-structured topics."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import paho.mqtt.client as mqtt

from .config import OutputConfig

logger = logging.getLogger("energy_hub.publisher")


class Publisher:
    """Publish engine snapshots to local MQTT as individual + aggregated topics."""

    def __init__(self, client: mqtt.Client, cfg: OutputConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._prefix = cfg.prefix.rstrip("/")
        self._last_publish = 0.0

    def should_publish(self) -> bool:
        return (time.time() - self._last_publish) >= self._cfg.publish_interval

    def publish_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Publish individual topics + the full aggregated blob."""
        prefix = self._prefix
        retain = self._cfg.retain

        # Individual topics
        self._pub(f"{prefix}/grid", snapshot.get("grid"), retain)
        self._pub(f"{prefix}/pv/roof", snapshot.get("pv_roof"), retain)
        self._pub(f"{prefix}/pv/balcony", snapshot.get("pv_balcony"), retain)
        self._pub(f"{prefix}/pv/total", snapshot.get("pv_total"), retain)
        self._pub(f"{prefix}/battery", snapshot.get("battery"), retain)
        self._pub(f"{prefix}/charger", snapshot.get("charger"), retain)
        self._pub(f"{prefix}/house", snapshot.get("house"), retain)

        # Full aggregated blob (for Telegraf / InfluxDB)
        self._pub(f"{prefix}/aggregated", snapshot, retain)

        self._last_publish = time.time()
        logger.debug(
            "Published: consumption=%.0fW grid=%.0fW pv=%.0fW",
            snapshot.get("consumption", 0),
            (snapshot.get("grid") or {}).get("power", 0),
            (snapshot.get("pv_total") or {}).get("power", 0),
        )

    def publish_health(self, health: dict[str, Any]) -> None:
        self._pub(f"{self._prefix}/health", health, retain=self._cfg.retain)

    def publish_alert(self, alert: dict[str, Any]) -> None:
        self._pub(f"{self._prefix}/alerts", alert, retain=False)

    def _pub(self, topic: str, payload: Any, retain: bool) -> None:
        if payload is None:
            return
        try:
            self._client.publish(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=retain,
            )
        except Exception:
            logger.exception("Failed to publish to %s", topic)
